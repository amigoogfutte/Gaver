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
import threading
from pathlib import Path

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import APP_URL, DOMAIN, MAX_BODY_BYTES, MAX_RECORD_BYTES, PANEL_URL_PATH, STORAGE_DIR

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
            # et budget kan deles med andre brugere (fx forældre om et barns gaver)
            return r.get("bruger") != uid and uid not in (r.get("deltMed") or []) and uid not in (r.get("inviteret") or [])
        if r.get("personId") in mine:
            if k == "koeb":
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
        for view in (SyncView(hass, store), MigView(hass, store), BrugereView(hass, store), LinkView(hass), AppRedirectView()):
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
