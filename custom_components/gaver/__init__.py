"""Gaver: fælles gaveliste. Gemmer data fra appen i Home Assistant og viser appen.

Alle brugere deler én liste. En bruger kan sætte "Det er mig" på en person. Den bruger får
aldrig købene til sig selv eller andres gaveidéer til sig selv – det filtreres her på serveren,
så det heller ikke ligger på telefonen.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import APP_URL, DOMAIN, MAX_BODY_BYTES, MAX_FIL_BYTES, MAX_RECORD_BYTES, PANEL_URL_PATH, STORAGE_DIR

_LOGGER = logging.getLogger(__name__)
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{3,160}$")
_USER_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
WWW = Path(__file__).parent / "www"
FAELLES = "faelles.json"
BRUGERE = "brugere.json"  # {bruger-id: {"admin": bool, "skjulIDeling": bool}} – sat af administratorer i appen
# Felter der bevares på en slettet post, så den stadig kan filtreres rigtigt.
META = ("kind", "personId", "anlId", "gaveId", "bruger", "deltMed", "inviteret", "oprettetAf", "koebtAf")


def _konverter(uid: str, poster: dict) -> list[dict]:
    """Gamle poster (én liste pr. bruger, v0.1–0.3) → fælles format."""
    ud: list[dict] = []
    for r in poster.values():
        if not isinstance(r, dict) or not isinstance(r.get("id"), str):
            continue
        r = dict(r)
        rid, upd, k = r["id"], r.get("updated", 1), r.get("kind")
        if r.get("deleted"):
            if k not in ("budget",):
                ud.append(r)
            continue
        if k == "person":
            b = r.pop("budget", 0)
            r.pop("skjult", None)
            if b:
                ud.append({"id": f"pbud-{uid}-{rid}", "updated": upd, "kind": "pbud", "bruger": uid, "personId": rid, "beloeb": b})
            ud.append(r)
        elif k == "budget":
            ud.append({"id": f"bud-{uid}-{r.get('personId')}-{r.get('anlId')}", "updated": upd, "kind": "budget", "bruger": uid,
                       "personId": r.get("personId"), "anlId": r.get("anlId"), "beloeb": r.get("beloeb", 0)})
        elif k == "gave":
            status = r.pop("status", "ide")
            r["oprettetAf"] = uid
            r.setdefault("antal", 1)
            ud.append(r)
            if status in ("koebt", "givet"):
                ud.append({"id": f"k-{rid}", "updated": upd, "kind": "koeb", "gaveId": rid, "personId": r.get("personId"),
                           "anlId": r.get("anlId"), "antal": 1, "pris": r.get("pris", 0), "koebtAf": uid, "status": status,
                           "oprettet": r.get("oprettet", upd)})
        else:
            ud.append(r)
    return ud


class Store:
    """Én fælles liste: /config/gaver/faelles.json med poster {id: {...,"updated": ms}}.

    Ved synkronisering vinder den udgave af en post, der senest er ændret. Slettede poster gemmes som {"deleted": true}.
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self._laas = threading.Lock()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        f = self.root / FAELLES
        if f.exists():
            return
        # Første gang: flyt de gamle lister (én pr. bruger) over i den fælles liste.
        data: dict[str, dict] = {}
        for gammel in sorted(self.root.glob("*.json")):
            uid = gammel.stem
            if not _USER_RE.match(uid):
                continue
            try:
                poster = json.loads(gammel.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(poster, dict):
                for r in _konverter(uid, poster):
                    cur = data.get(r["id"])
                    if cur is None or r.get("updated", 0) > cur.get("updated", 0):
                        data[r["id"]] = r
            gammel.rename(gammel.with_suffix(".json.gammel"))
            _LOGGER.info("Gaver: flyttede %s over i den fælles liste", gammel.name)
        self._skriv(data)

    # ---- administratorer i appen (ud over Home Assistants egne administratorer) ----
    def _brugere(self) -> dict:
        f = self.root / BRUGERE
        try:
            d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except (OSError, ValueError):
            d = {}
        return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}

    def app_admins(self) -> set[str]:
        return {k for k, v in self._brugere().items() if v.get("admin")}

    def skjult_i_deling(self) -> set[str]:
        """Brugere som en administrator har valgt ikke skal vises, når man deler budget."""
        return {k for k, v in self._brugere().items() if v.get("skjulIDeling")}

    def saet_bruger(self, uid: str, felter: dict) -> None:
        with self._laas:
            f = self.root / BRUGERE
            d = self._brugere()
            d.setdefault(uid, {}).update({k: bool(v) for k, v in felter.items() if k in ("admin", "skjulIDeling")})
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, f)

    def claims(self) -> dict[str, str]:
        """bruger-id → personens navn for alle "Det er mig"."""
        with self._laas:
            return {r["brugerId"]: r.get("navn", "") for r in self._laes().values()
                    if r.get("kind") == "person" and not r.get("deleted") and r.get("brugerId")}

    # ---- kvitteringer (billeder/PDF) i /config/gaver/filer ----
    FILTYPER = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}

    def _fil_sti(self, fid: str) -> Path | None:
        d = self.root / "filer"
        for ext in self.FILTYPER.values():
            f = d / f"{fid}{ext}"
            if f.exists():
                return f
        return None

    def _slet_fil(self, fid: str) -> None:
        f = self._fil_sti(fid)
        if f:
            f.unlink(missing_ok=True)
        (self.root / "filer" / f"{fid}.json").unlink(missing_ok=True)

    def gem_fil(self, uid: str, fid: str, gave_id: str, typ: str, indhold: bytes) -> str | None:
        """Gem en kvittering til en gave. Returnerer en fejltekst eller None."""
        with self._laas:
            data = self._laes()
            g = data.get(gave_id)
            if not g or g.get("deleted") or g.get("kind") != "gave":
                return "Gaven findes ikke – vent til den er synkroniseret"
            if g.get("personId") in self._mine(data, uid):
                return "Du kan ikke lægge kvitteringer på dine egne gaver"
            cur = data.get(fid)
            if cur is not None and (cur.get("deleted") or cur.get("oprettetAf") != uid):
                return "Findes allerede"
            d = self.root / "filer"
            d.mkdir(parents=True, exist_ok=True)
            self._slet_fil(fid)
            (d / f"{fid}{self.FILTYPER[typ]}").write_bytes(indhold)
            (d / f"{fid}.json").write_text(json.dumps({"personId": g.get("personId"), "bruger": uid, "type": typ}), encoding="utf-8")
        return None

    def hent_fil(self, uid: str, fid: str) -> tuple[bytes, str] | None:
        with self._laas:
            data = self._laes()
            f = self._fil_sti(fid)
            if not f:
                return None
            r = data.get(fid)
            try:
                meta = json.loads((self.root / "filer" / f"{fid}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            if r is not None:
                if r.get("deleted") or self._skjult(r, self._mine(data, uid), uid):
                    return None
            elif meta.get("bruger") != uid:
                return None  # endnu ikke synkroniseret: kun den der lagde den ind
            return f.read_bytes(), meta.get("type") or "application/octet-stream"

    # ---- indbakke: kvitteringer sendt fra iPhone-genvejen (Del → Send til Gaver) ----
    _IND_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")

    def _ind_dir(self) -> Path:
        return self.root / "indbakke"

    def _adresser(self) -> dict[str, str]:
        try:
            return json.loads((self._ind_dir() / "adresser.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def ind_noegle(self, uid: str, ny: bool = False) -> str:
        """Den hemmelige nøgle i brugerens genvejs-adresse (laves første gang)."""
        with self._laas:
            a = self._adresser()
            gammel = next((k for k, v in a.items() if v == uid), None)
            if gammel and not ny:
                return gammel
            if gammel:
                a.pop(gammel, None)
            n = secrets.token_urlsafe(24)
            a[n] = uid
            self._ind_dir().mkdir(parents=True, exist_ok=True)
            (self._ind_dir() / "adresser.json").write_text(json.dumps(a), encoding="utf-8")
            return n

    @staticmethod
    def _gaet_type(indhold: bytes, typ: str) -> str | None:
        if indhold[:5] == b"%PDF-":
            return "application/pdf"
        if indhold[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if indhold[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if indhold[:4] == b"RIFF" and indhold[8:12] == b"WEBP":
            return "image/webp"
        if indhold[4:8] == b"ftyp" and indhold[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heis", b"hevc"):
            return "image/heic"
        return None

    INDTYPER = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic"}

    def ind_gem(self, noegle: str, indhold: bytes, typ: str, navn: str) -> str | None:
        with self._laas:
            uid = self._adresser().get(noegle)
            if not uid:
                return "ukendt"
            t = self._gaet_type(indhold, typ)
            if not t:
                return "type"
            d = self._ind_dir() / uid
            d.mkdir(parents=True, exist_ok=True)
            if len(list(d.glob("*.json"))) >= 50:
                return "fuld"
            fid = f"ind-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
            (d / f"{fid}{self.INDTYPER[t]}").write_bytes(indhold)
            (d / f"{fid}.json").write_text(json.dumps({"id": fid, "type": t, "navn": navn[:120], "tid": int(time.time() * 1000), "str": len(indhold)}), encoding="utf-8")
        return None

    def ind_liste(self, uid: str) -> list[dict]:
        d = self._ind_dir() / uid
        ud = []
        for m in d.glob("*.json") if d.exists() else []:
            try:
                ud.append(json.loads(m.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(ud, key=lambda x: x.get("tid", 0))

    def ind_hent(self, uid: str, fid: str) -> tuple[bytes, str] | None:
        if not _ID_RE.match(fid):
            return None
        d = self._ind_dir() / uid
        try:
            meta = json.loads((d / f"{fid}.json").read_text(encoding="utf-8"))
            return (d / f"{fid}{self.INDTYPER[meta['type']]}").read_bytes(), meta["type"]
        except (OSError, ValueError, KeyError):
            return None

    def ind_slet(self, uid: str, fid: str) -> None:
        if not _ID_RE.match(fid):
            return
        d = self._ind_dir() / uid
        for f in d.glob(f"{fid}.*") if d.exists() else []:
            f.unlink(missing_ok=True)

    def _laes(self) -> dict[str, dict]:
        f = self.root / FAELLES
        if not f.exists():
            return {}
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _LOGGER.warning("Kunne ikke læse %s", f)
            return {}

    def _skriv(self, data: dict) -> None:
        f = self.root / FAELLES
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)

    @staticmethod
    def _mine(data: dict, uid: str) -> set[str]:
        return {r["id"] for r in data.values() if r.get("kind") == "person" and not r.get("deleted") and r.get("brugerId") == uid}

    @staticmethod
    def _skjult(r: dict, mine: set[str], uid: str) -> bool:
        """Må brugeren ikke se denne post?"""
        k = r.get("kind")
        if k in ("budget", "pbud"):
            # et budget kan deles med andre brugere (fx forældre om et barns gaver),
            # men aldrig med den person, budgettet handler om
            if r.get("personId") in mine:
                return True
            return r.get("bruger") != uid and uid not in (r.get("deltMed") or []) and uid not in (r.get("inviteret") or [])
        if k == "abud":
            return r.get("bruger") != uid  # budget for en hel anledning er personligt
        if r.get("personId") in mine:
            if k in ("koeb", "kvit"):
                return True
            if k == "gave" and r.get("oprettetAf") != uid:
                return True
        return False

    def sync(self, uid: str, navn: str, admin: bool, indkomne: list) -> list[dict]:
        """Flet brugerens ændringer ind og returnér de poster, brugeren må se."""
        skjulte_i_deling = self.skjult_i_deling()
        with self._laas:
            data = self._laes()
            aendret = False
            for r in indkomne:
                if not isinstance(r, dict):
                    continue
                rid, upd = r.get("id"), r.get("updated")
                if not isinstance(rid, str) or not _ID_RE.match(rid) or not isinstance(upd, (int, float)):
                    continue
                if len(json.dumps(r, ensure_ascii=False)) > MAX_RECORD_BYTES:
                    continue
                cur = data.get(rid)
                if cur is not None and upd <= cur.get("updated", 0):
                    continue
                mine = self._mine(data, uid)
                if cur is not None and self._skjult(cur, mine, uid):
                    continue  # man kan ikke røre poster, man ikke må se
                r = dict(r)
                if r.get("deleted") and cur is not None:
                    for m in META:
                        if m in cur:
                            r[m] = cur[m]
                k = (cur or r).get("kind")
                if k in ("budget", "pbud") and cur is not None and uid != cur.get("bruger") and uid not in (cur.get("deltMed") or []):
                    # kun inviteret: man kan kun godkende eller afvise
                    if r.get("deleted") or uid not in (cur.get("inviteret") or []):
                        continue
                    ja = uid in (r.get("deltMed") or [])
                    r = dict(cur, id=rid, updated=upd)
                    r["inviteret"] = sorted(set(cur.get("inviteret") or []) - {uid})
                    r["deltMed"] = sorted(set(cur.get("deltMed") or []) | ({uid} if ja else set()))
                    data[rid] = r
                    aendret = True
                    continue
                if r.get("deleted"):
                    if k in ("person", "anl") and not admin:
                        continue
                    if k == "kvit":
                        if cur is not None and cur.get("oprettetAf") != uid and not admin:
                            continue
                        self._slet_fil(rid)
                    if k == "koeb" and cur is not None and cur.get("koebtAf") != uid and uid not in (cur.get("fraGruppe") or []) and not admin:
                        continue
                elif k == "person":
                    gl = (cur or {}).get("brugerId") or ""
                    ny = r.get("brugerId") or ""
                    # Man kan kun sige "Det er mig" om én person, og kun en administrator kan åbne for det igen.
                    if ny != gl and not ((gl == "" and ny == uid and not mine - {rid}) or (admin and ny == "")):
                        ny = gl
                    r["brugerId"] = ny
                    if not ny:
                        r.pop("brugerNavn", None)
                    elif ny == uid:
                        r["brugerNavn"] = navn
                    elif cur is not None and cur.get("brugerNavn"):
                        r["brugerNavn"] = cur["brugerNavn"]
                elif k == "gave":
                    r["oprettetAf"] = cur.get("oprettetAf", uid) if cur else uid
                elif k == "abud":
                    r["bruger"] = cur.get("bruger", uid) if cur else uid
                elif k == "kvit":
                    if cur is not None and cur.get("oprettetAf") != uid and not admin:
                        continue
                    r["oprettetAf"] = cur.get("oprettetAf", uid) if cur else uid
                elif k == "koeb":
                    if cur is not None and cur.get("koebtAf") != uid and uid not in (cur.get("fraGruppe") or []) and not admin:
                        continue
                    r["koebtAf"] = cur.get("koebtAf", uid) if cur else uid
                    if not cur:
                        r["koebtNavn"] = navn
                    elif cur.get("koebtNavn"):
                        r["koebtNavn"] = cur["koebtNavn"]
                elif k in ("budget", "pbud"):
                    # Nye brugere i deltMed bliver først inviteret – de skal selv godkende.
                    ejer = cur.get("bruger", uid) if cur else uid
                    r["bruger"] = ejer
                    r["ejerNavn"] = cur.get("ejerNavn", navn) if cur else navn
                    gl_delt = set((cur or {}).get("deltMed") or [])
                    gl_inv = set((cur or {}).get("inviteret") or [])
                    oensket = {d for d in (r.get("deltMed") or []) if isinstance(d, str) and _USER_RE.match(d)} - {ejer}
                    modtager = (data.get(r.get("personId") or (cur or {}).get("personId") or "") or {}).get("brugerId")
                    if modtager:
                        oensket.discard(modtager)  # man kan ikke dele budgettet med den, det handler om
                    delt = gl_delt & oensket
                    nye = oensket - delt - gl_inv
                    nye -= skjulte_i_deling
                    r["deltMed"] = sorted(delt)
                    r["inviteret"] = sorted((gl_inv & oensket) | nye)
                if self._skjult(r, mine, uid):
                    continue  # fx et køb til en selv
                data[rid] = r
                aendret = True
            if aendret:
                self._skriv(data)
            mine = self._mine(data, uid)
            return [r for r in data.values() if not self._skjult(r, mine, uid)]


class SyncView(HomeAssistantView):
    """GET: alle synlige poster. POST {"poster":[...]}: flet ind og få alle synlige poster tilbage."""

    url = "/api/gaver/sync"
    name = "api:gaver:sync"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def _kør(self, request: web.Request, poster: list) -> web.Response:
        u = request["hass_user"]
        admin = await er_admin(self.hass, self.store, u)
        data = await self.hass.async_add_executor_job(self.store.sync, u.id, u.name or "", admin, poster)
        return self.json({"poster": data, "fuld": True})

    async def get(self, request: web.Request) -> web.Response:
        return await self._kør(request, [])

    async def post(self, request: web.Request) -> web.Response:
        body = await request.read()
        if len(body) > MAX_BODY_BYTES:
            return self.json_message("For stor", 413)
        try:
            obj = json.loads(body)
        except ValueError:
            return self.json_message("Ugyldig JSON", 400)
        poster = obj.get("poster") if isinstance(obj, dict) else None
        if not isinstance(poster, list):
            return self.json_message("Forkert indhold", 400)
        return await self._kør(request, poster)


async def er_admin(hass: HomeAssistant, store: Store, u) -> bool:
    if u.is_admin:
        return True
    return u.id in await hass.async_add_executor_job(store.app_admins)


class MigView(HomeAssistantView):
    """Hvem er logget ind."""

    url = "/api/gaver/mig"
    name = "api:gaver:mig"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request) -> web.Response:
        u = request["hass_user"]
        return self.json({"id": u.id, "navn": u.name, "admin": await er_admin(self.hass, self.store, u)})


class BrugereView(HomeAssistantView):
    """GET: alle brugere (til at dele budget; administratorer får også admin-oplysninger).
    POST {"id", "admin"?, "skjulIDeling"?} (kun administratorer): administrator / vis ikke ved deling af budget."""

    url = "/api/gaver/brugere"
    name = "api:gaver:brugere"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request) -> web.Response:
        u = request["hass_user"]
        admin = await er_admin(self.hass, self.store, u)
        app = await self.hass.async_add_executor_job(self.store.app_admins)
        skjul = await self.hass.async_add_executor_job(self.store.skjult_i_deling)
        claims = await self.hass.async_add_executor_job(self.store.claims)
        ud = []
        for b in await self.hass.auth.async_get_users():
            if b.system_generated or not b.is_active:
                continue
            if admin:
                ud.append({"id": b.id, "navn": b.name or "", "haAdmin": bool(b.is_admin), "admin": bool(b.is_admin) or b.id in app,
                           "person": claims.get(b.id, ""), "skjulIDeling": b.id in skjul})
            elif b.id not in skjul:
                ud.append({"id": b.id, "navn": b.name or ""})
        ud.sort(key=lambda x: x["navn"].lower())
        return self.json({"brugere": ud})

    async def post(self, request: web.Request) -> web.Response:
        u = request["hass_user"]
        if not await er_admin(self.hass, self.store, u):
            return self.json_message("Kun for administratorer", 403)
        try:
            obj = await request.json()
        except ValueError:
            return self.json_message("Ugyldig JSON", 400)
        bid = obj.get("id") if isinstance(obj, dict) else None
        if not isinstance(bid, str) or not _USER_RE.match(bid):
            return self.json_message("Ukendt bruger", 400)
        if bid == u.id and "admin" in obj and not obj.get("admin"):
            return self.json_message("Du kan ikke fjerne dig selv", 400)
        await self.hass.async_add_executor_job(self.store.saet_bruger, bid, obj)
        return self.json({"ok": True})


class FilView(HomeAssistantView):
    """POST /api/gaver/fil/<id>?gave=<gaveId> (rå billede/PDF), GET /api/gaver/fil/<id>."""

    url = "/api/gaver/fil/{fid}"
    name = "api:gaver:fil"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request, fid: str) -> web.Response:
        if not _ID_RE.match(fid):
            return self.json_message("Ugyldig", 400)
        res = await self.hass.async_add_executor_job(self.store.hent_fil, request["hass_user"].id, fid)
        if not res:
            return self.json_message("Findes ikke", 404)
        indhold, typ = res
        return web.Response(body=indhold, content_type=typ, headers={"Cache-Control": "private, max-age=86400"})

    async def post(self, request: web.Request, fid: str) -> web.Response:
        gave_id = request.query.get("gave", "")
        typ = (request.content_type or "").lower()
        if not _ID_RE.match(fid) or not _ID_RE.match(gave_id):
            return self.json_message("Ugyldig", 400)
        if typ not in Store.FILTYPER:
            return self.json_message("Kun billeder og PDF", 415)
        indhold = await request.read()
        if not indhold or len(indhold) > MAX_FIL_BYTES:
            return self.json_message("Filen er for stor (højst 10 MB)", 413)
        fejl = await self.hass.async_add_executor_job(self.store.gem_fil, request["hass_user"].id, fid, gave_id, typ, indhold)
        if fejl:
            return self.json_message(fejl, 400)
        return self.json({"ok": True})


class IndView(HomeAssistantView):
    """Offentlig adresse til iPhone-genvejen: POST /api/gaver/ind/<hemmelig nøgle> med filen som indhold."""

    url = "/api/gaver/ind/{noegle}"
    name = "api:gaver:ind"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def post(self, request: web.Request, noegle: str) -> web.Response:
        if not Store._IND_RE.match(noegle):
            return web.Response(status=404, text="Ukendt adresse")
        indhold = await request.read()
        if not indhold:
            return web.Response(status=400, text="Der kom ingen fil med – vælg Genvejsinput som Fil i genvejen")
        if len(indhold) > MAX_FIL_BYTES:
            return web.Response(status=413, text="Filen er for stor (højst 10 MB)")
        navn = request.headers.get("X-Filnavn") or request.query.get("navn") or ""
        fejl = await self.hass.async_add_executor_job(self.store.ind_gem, noegle, indhold, request.content_type or "", navn)
        if fejl == "ukendt":
            return web.Response(status=404, text="Adressen er ikke gyldig længere – kopiér den nye fra Gaver")
        if fejl == "type":
            return web.Response(status=415, text="Gaver kan kun modtage PDF og billeder")
        if fejl == "fuld":
            return web.Response(status=507, text="Indbakken er fuld – læg nogle af kvitteringerne på gaver først")
        return web.Response(text="✓ Kvitteringen er sendt til Gaver")

    async def get(self, request: web.Request, noegle: str) -> web.Response:
        return web.Response(text="Gaver: Brug adressen i en genvej (POST).")


class IndbakkeView(HomeAssistantView):
    """GET liste + din genvejs-adresse; POST {ny:true} laver en ny adresse."""

    url = "/api/gaver/indbakke"
    name = "api:gaver:indbakke"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request) -> web.Response:
        uid = request["hass_user"].id
        liste = await self.hass.async_add_executor_job(self.store.ind_liste, uid)
        noegle = await self.hass.async_add_executor_job(self.store.ind_noegle, uid)
        return self.json({"poster": liste, "noegle": noegle})

    async def post(self, request: web.Request) -> web.Response:
        uid = request["hass_user"].id
        noegle = await self.hass.async_add_executor_job(self.store.ind_noegle, uid, True)
        return self.json({"noegle": noegle})


class IndFilView(HomeAssistantView):
    url = "/api/gaver/indbakke/{fid}"
    name = "api:gaver:indbakke:fil"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request, fid: str) -> web.Response:
        res = await self.hass.async_add_executor_job(self.store.ind_hent, request["hass_user"].id, fid)
        if not res:
            return self.json_message("Findes ikke", 404)
        return web.Response(body=res[0], content_type=res[1], headers={"Cache-Control": "private, max-age=3600"})

    async def delete(self, request: web.Request, fid: str) -> web.Response:
        await self.hass.async_add_executor_job(self.store.ind_slet, request["hass_user"].id, fid)
        return self.json({"ok": True})


class LinkView(HomeAssistantView):
    """Home Assistants adresse udefra (egen ekstern adresse eller Nabu Casa), til del-linket."""

    url = "/api/gaver/link"
    name = "api:gaver:link"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        from homeassistant.helpers.network import NoURLAvailableError, get_url

        try:
            base = get_url(self.hass, allow_internal=False, allow_ip=False)
        except NoURLAvailableError:
            base = None
        return self.json({"url": base})


class AppRedirectView(HomeAssistantView):
    """Kort adresse: /gaver-start → appen."""

    url = "/gaver-start"
    name = "gaver:start"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{APP_URL}/index.html")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sæt integrationen op."""
    store = Store(hass.config.path(STORAGE_DIR))
    await hass.async_add_executor_job(store.ensure)

    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("http_registered"):
        # HTTP-ruter kan ikke fjernes igen, så de registreres kun én gang pr. opstart.
        await hass.http.async_register_static_paths([StaticPathConfig(APP_URL, str(WWW), False)])
        for view in (SyncView(hass, store), MigView(hass, store), BrugereView(hass, store), LinkView(hass), FilView(hass, store), IndView(hass, store), IndbakkeView(hass, store), IndFilView(hass, store), AppRedirectView()):
            hass.http.register_view(view)
        data["http_registered"] = True

    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="Gaver",
        sidebar_icon="mdi:gift-outline",
        frontend_url_path=PANEL_URL_PATH,
        config={"url": f"{APP_URL}/index.html"},
        require_admin=False,
        update=True,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Fjern sidepanelet."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    return True
