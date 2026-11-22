"""Security: the cve-scanner's findings, Claude's verdicts, and the pings.

The cve-scanner (``/services/cve-scanner``) writes ``data/findings.json``
once a night. Every ``SECURITY_SYNC_INTERVAL`` seconds (30 minutes by
default) the dashboard reads it and:

- keeps Vikunja's Security project in step (``cards.py``): one card per
  service with open findings, which the claude-dispatcher's ``security``
  profile assesses and labels ``Act now``, ``Next upgrade`` or ``Not affected``;
- pings Discord straight away for an Act now verdict, or for a known-exploited
  CVE on a public service (``notify.py``).

The findings and verdicts show up in the Updates tile, the Updates table, a
Security tab on each service, and ``GET /internal/security/annotations``,
which the tracker's digest reads for its Security section.

The scan views work as soon as the scanner has written a file. The cards need
``VIKUNJA_API_TOKEN`` and ``SECURITY_PROJECT_ID``; the pings need
``DISCORD_SECURITY_WEBHOOK_URL``. Nothing here runs in mock mode, where the
routes serve fixtures.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from .. import inventory, mock, models
from ..integrations._common import AsyncTTLCache
from ..upgrades.vikunja import Card, Vikunja
from . import cards, findings, notify, verdicts

log = logging.getLogger("alpenglow.security")
router = APIRouter(tags=["security"])

_BOARD_CACHE = AsyncTTLCache(ttl=60.0)
_sync_lock = asyncio.Lock()
_scan_cache: tuple[tuple, findings.Scan | None] | None = None


@dataclass(frozen=True)
class Settings:
    findings_path: Path
    ignore_path: Path
    epss_threshold: float = 0.1
    sync_interval: int = 1800
    # The Security board on Vikunja; off when the token or project is missing.
    vikunja_url: str = "http://vikunja:3456/api/v1"
    token: str | None = None
    project_id: int | None = None
    public_url: str | None = None
    # Discord pings; off without a webhook.
    webhook: str | None = None
    mention: str | None = None
    pings_path: Path = Path("/data-store/security-pings.json")
    dashboard_url: str | None = None
    repo_url: str | None = None  # for compose-file links on cards

    @property
    def board(self) -> bool:
        return bool(self.token and self.project_id)

    @classmethod
    def from_env(cls) -> Settings | None:
        if mock.mock_enabled():
            return None
        env = os.environ.get
        root = inventory.services_root() / "cve-scanner"
        project = env("SECURITY_PROJECT_ID")
        return cls(
            findings_path=Path(env("SECURITY_FINDINGS_PATH") or root / "data" / "findings.json"),
            ignore_path=Path(env("SECURITY_IGNORE_PATH") or root / "ignore.yaml"),
            epss_threshold=float(env("SECURITY_EPSS_THRESHOLD") or 0.1),
            sync_interval=int(env("SECURITY_SYNC_INTERVAL") or 1800),
            vikunja_url=(env("VIKUNJA_API_URL") or "http://vikunja:3456/api/v1").rstrip("/"),
            token=env("VIKUNJA_API_TOKEN") or None,
            project_id=int(project) if project else None,
            public_url=(env("VIKUNJA_PUBLIC_URL") or "").rstrip("/") or None,
            webhook=env("DISCORD_SECURITY_WEBHOOK_URL") or None,
            mention=env("DISCORD_SECURITY_MENTION") or None,
            pings_path=Path(env("SECURITY_PINGS_PATH") or "/data-store/security-pings.json"),
            dashboard_url=(env("DASHBOARD_PUBLIC_URL") or "").rstrip("/") or None,
            repo_url=(env("SECURITY_REPO_URL") or "https://github.com/gizmo385/alpenglow/blob/main").rstrip("/"),
        )


def _api(s: Settings, client: httpx.AsyncClient) -> Vikunja:
    assert s.project_id is not None
    return Vikunja(s.vikunja_url, s.token or "", s.project_id, client)


# ── the scan ─────────────────────────────────────────────────────────────────


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def load_scan(s: Settings | None = None) -> findings.Scan | None:
    """The latest scan, re-read only when the file or the ignore list changes."""
    global _scan_cache
    s = s or Settings.from_env()
    if s is None:
        return None
    key = (str(s.findings_path), _mtime(s.findings_path), _mtime(s.ignore_path), s.epss_threshold)
    if _scan_cache is None or _scan_cache[0] != key:
        _scan_cache = (key, findings.load(s.findings_path, s.ignore_path, s.epss_threshold))
    return _scan_cache[1]


# ── the board ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Board:
    cards: list[cards.SecurityCard]
    comments: dict[int, list[dict]]
    verdicts: list[verdicts.Verdict]


EMPTY_BOARD = Board([], {}, [])


async def _read_board(s: Settings, client: httpx.AsyncClient) -> Board:
    api = _api(s, client)
    scs = [cards.SecurityCard.parse(c) for c in await api.cards()]
    open_ids = [sc.card.id for sc in scs if not sc.card.done and sc.service]
    got = await asyncio.gather(*(api.comments(i) for i in open_ids))
    comments = dict(zip(open_ids, got))
    return Board(scs, comments, verdicts.from_cards(scs, comments, s.public_url))


async def _load_board() -> Board:
    s = Settings.from_env()
    if s is None or not s.board:
        return EMPTY_BOARD
    async with httpx.AsyncClient(timeout=30) as client:
        return await _read_board(s, client)


async def board() -> Board:
    """The Security cards and verdicts; empty when off or unreachable."""
    try:
        return await _BOARD_CACHE.get(_load_board)
    except (httpx.HTTPError, ValueError) as err:
        log.warning("security: couldn't read the board: %s", err)
        return EMPTY_BOARD


async def verdicts_by_service() -> dict[str, verdicts.Verdict]:
    out: dict[str, verdicts.Verdict] = {}
    for v in (await board()).verdicts:  # most urgent first, so keep the first
        out.setdefault(v.service, v)
    return out


# ── what other views read ────────────────────────────────────────────────────


def _counts(scan: findings.Scan | None, by_service: dict[str, verdicts.Verdict]) -> models.SecurityCounts:
    c = models.SecurityCounts()
    services = scan.services if scan else {}
    for sid, sf in services.items():
        if not sf.open:
            continue
        group = by_service[sid].group if sid in by_service else verdicts.WAITING
        if group == verdicts.ACT_NOW:
            c.actNow += 1
        elif group == verdicts.NEXT_UPGRADE:
            c.nextUpgrade += 1
        elif group == verdicts.NOT_AFFECTED:
            c.notAffected += 1
        else:
            c.waiting += 1
    return c


async def counts() -> models.SecurityCounts | None:
    """Services with open findings, by verdict. ``None`` before the first scan."""
    if mock.mock_enabled():
        return _mock_counts()
    scan = load_scan()
    if scan is None:
        return None
    return _counts(scan, await verdicts_by_service())


async def entry_fields() -> dict[str, dict]:
    """``{sid: UpdateEntry security fields}`` for services with open findings."""
    if mock.mock_enabled():
        return _mock_entry_fields()
    scan = load_scan()
    if scan is None:
        return {}
    by_service = await verdicts_by_service()
    out = {}
    for sid, sf in scan.services.items():
        if not sf.open:
            continue
        v = by_service.get(sid)
        group = v.group if v else verdicts.WAITING
        out[sid] = {
            "securityVerdict": group,
            "securityNote": (v.note if v and v.note else None) or _waiting_note(sf),
            "securityColor": verdicts.COLORS[group],
            "securityCardUrl": v.url if v else None,
            "securityOpen": len(sf.open),
        }
    return out


def _waiting_note(sf: findings.ServiceFindings) -> str:
    n = len(sf.open)
    worst = (sf.worst or "").lower()
    kev = " (known exploited)" if any(o.finding.kev for o in sf.open) else ""
    return f"{n} open finding{'s' if n != 1 else ''}, worst {worst}{kev}"


# ── sync ─────────────────────────────────────────────────────────────────────


async def _contexts(scan: findings.Scan, s: Settings) -> dict[str, cards.Context]:
    from ..integrations import updates  # avoids an import cycle

    repo = inventory.scan_repo()
    try:
        enrich = await updates.service_enrichment()
    except Exception:
        enrich = {}
    out = {}
    for sid in scan.services:
        rs = repo.get(sid)
        name, _icon = updates._meta_for(sid)
        pending = None
        if sid in enrich and enrich[sid].get("latestVersion"):
            pending = f"to {enrich[sid]['latestVersion']}"
        compose = f"{s.repo_url}/{sid}/compose.yaml" if s.repo_url and rs else None
        out[sid] = cards.Context(name=name, tier=rs.tier if rs else "internal", url=rs.url if rs else None,
                                 pending=pending, compose_url=compose)
    return out


async def sync_once() -> dict | None:
    """Sync the board and send any pings now. ``None`` before the first scan."""
    s = Settings.from_env()
    if s is None:
        return None
    scan = load_scan(s)
    if scan is None:
        return None
    contexts = await _contexts(scan, s)
    done: dict = {"filed": 0, "updated": 0, "closed": 0, "pinged": 0}
    async with _sync_lock, httpx.AsyncClient(timeout=30) as client:
        current = EMPTY_BOARD
        if s.board:
            todo = await cards.sync(scan.services, contexts, _api(s, client))
            done.update(filed=len(todo.create), updated=len(todo.add), closed=len(todo.close))
            _BOARD_CACHE.invalidate()
            current = await _read_board(s, client)
        if s.webhook:
            pings = notify.due(current.verdicts, scan.services,
                               {sid: c.tier for sid, c in contexts.items()},
                               {sid: c.name for sid, c in contexts.items()},
                               s.dashboard_url, notify.load_sent(s.pings_path))
            if pings:
                done["pinged"] = await notify.send(client, s.webhook, pings, s.mention, s.pings_path)
    return done


async def _loop(interval: int) -> None:
    await asyncio.sleep(45)  # let the tracker and the upgrades sync go first
    while True:
        try:
            done = await sync_once()
            if done and any(done.values()):
                log.info("security: %(filed)d filed, %(updated)d updated, %(closed)d closed, "
                         "%(pinged)d pinged", done)
        except Exception:
            log.exception("security: sync failed; trying again next interval")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(app):
    s = Settings.from_env()
    task = asyncio.create_task(_loop(s.sync_interval), name="alpenglow-security-sync") if s else None
    try:
        yield
    finally:
        if task:
            task.cancel()


# ── views ────────────────────────────────────────────────────────────────────


def _finding(o: findings.OpenFinding, ignored: str | None = None) -> models.SecurityFinding:
    f = o.finding
    return models.SecurityFinding(
        id=f.id, aliases=list(f.aliases), severity=f.severity, package=f.package, version=f.version,
        type=f.type or f.source, fixedIn=list(f.fixed_in), kev=f.kev, epss=f.epss,
        epssPercentile=f.epss_percentile, url=f.url, summary=f.summary, image=o.image, why=o.why,
        ignoredReason=ignored,
    )


def summary(scan: findings.Scan | None, by_service: dict[str, verdicts.Verdict],
            names: dict[str, str]) -> models.SecuritySummary:
    """Pure: the ``/api/security`` view."""
    if scan is None:
        return models.SecuritySummary(scannedAt=None, counts=models.SecurityCounts(), services=[])
    rows = []
    for sid, sf in scan.services.items():
        if not sf.open:
            continue
        v = by_service.get(sid)
        group = v.group if v else verdicts.WAITING
        rows.append(models.SecurityServiceRow(
            id=sid, name=names.get(sid, sid), open=len(sf.open), worst=sf.worst,
            kev=any(o.finding.kev for o in sf.open), verdict=group,
            verdictNote=(v.note if v else "") or None, verdictColor=verdicts.COLORS[group],
            cardUrl=v.url if v else None,
        ))
    order = {g["name"]: i for i, g in enumerate(verdicts.GROUPS)}
    rows.sort(key=lambda r: (order.get(r.verdict or "", 9), not r.kev,
                             findings.severity_rank(r.worst or ""), r.name.casefold()))
    return models.SecuritySummary(
        scannedAt=scan.scanned_at, dbBuilt=scan.db_built, errors=scan.errors,
        counts=_counts(scan, by_service), services=rows, images=len(scan.images),
        unscanned=[i.ref for i in scan.images if i.error],
    )


def service_view(sid: str, scan: findings.Scan | None, b: Board, tier: str,
                 public_url: str | None) -> models.ServiceSecurity:
    """Pure: the ``/api/services/{id}/security`` view."""
    sf = scan.services.get(sid) if scan else None
    v = next((x for x in b.verdicts if x.service == sid), None)
    open_card = next((sc for sc in b.cards if sc.service == sid and not sc.card.done), None)
    closed = [models.SecurityClosedCard(title=sc.card.title, url=verdicts.card_url(public_url, sc.card.id))
              for sc in b.cards if sc.service == sid and sc.card.done]
    closed.sort(key=lambda c: c.url or "", reverse=True)
    reply = verdicts.latest_reply(b.comments.get(open_card.card.id, [])) if open_card else ""
    return models.ServiceSecurity(
        id=sid,
        scannedAt=scan.scanned_at if scan else None,
        tier=tier,
        images=[models.SecurityImage(ref=i.ref, digest=i.digest, localBuild=i.local_build, packages=i.packages,
                                     error=i.error, containers=list(i.containers))
                for i in (sf.images if sf else [])],
        open=[_finding(o) for o in (sf.open if sf else [])],
        ignored=[_finding(o, i.reason or "no reason given") for o, i in (sf.ignored if sf else [])],
        belowBar=sf.below_bar if sf else 0,
        verdict=v.group if v else (verdicts.WAITING if sf and sf.open else None),
        verdictNote=(v.note if v else "") or None,
        verdictColor=verdicts.COLORS[v.group] if v else None,
        reply=reply or None,
        cardUrl=v.url if v else None,
        canAsk=open_card is not None,
        closed=closed[:10],
    )


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/api/security")
async def get_security() -> models.SecuritySummary:
    if mock.mock_enabled():
        return _mock_summary()
    from ..integrations import updates  # avoids an import cycle

    scan = load_scan()
    names = {sid: updates._meta_for(sid)[0] for sid in (scan.services if scan else {})}
    return summary(scan, await verdicts_by_service(), names)


@router.get("/api/services/{service_id}/security")
async def get_service_security(service_id: str) -> models.ServiceSecurity:
    if mock.mock_enabled():
        return _mock_service(service_id)
    repo = inventory.scan_repo()
    if service_id not in repo:
        raise HTTPException(404, f"no service {service_id!r}")
    s = Settings.from_env()
    return service_view(service_id, load_scan(s), await board(), repo[service_id].tier,
                        s.public_url if s else None)


@router.post("/api/services/{service_id}/security/ask")
async def post_ask(service_id: str, body: models.SecurityAsk) -> dict:
    """Ask Claude a follow-up on the service's open card (an ``@claude`` comment)."""
    question = " ".join(body.question.split())
    if not question:
        raise HTTPException(422, "ask a question")
    if len(question) > 2000:
        raise HTTPException(422, "keep the question under 2,000 characters")
    if mock.mock_enabled():
        return {"ok": True}
    s = Settings.from_env()
    if s is None or not s.board:
        raise HTTPException(409, "the Security board is off")
    b = await board()
    sc = next((c for c in b.cards if c.service == service_id and not c.card.done), None)
    if sc is None:
        raise HTTPException(409, "this service has no open Security card to ask about")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await _api(s, client).comment(Card(sc.card.id, "", "", False), f"@claude {question}")
    except httpx.HTTPError as err:
        raise HTTPException(502, f"couldn't comment on the card: {err}") from err
    _BOARD_CACHE.invalidate()
    return {"ok": True, "cardUrl": verdicts.card_url(s.public_url, sc.card.id)}


@router.post("/api/security/sync")
async def post_sync() -> dict:
    try:
        done = await sync_once()
    except httpx.HTTPError as err:
        raise HTTPException(502, f"sync failed: {err}") from err
    if done is None:
        raise HTTPException(409, "no scan yet: the cve-scanner hasn't written findings.json")
    return done


@router.get("/internal/security/annotations")
async def get_annotations() -> dict:
    """The tracker's extra-annotations format (image-updates-tracker, ``EXTRA_ANNOTATIONS_URLS``).

    Outside ``/api`` because the tracker can't pass oauth2-proxy. Read-only:
    card titles, verdicts, one-line notes and links.
    """
    from ..integrations import updates  # avoids an import cycle

    if mock.mock_enabled():
        return verdicts.payload([], {})
    repos: dict[str, str] = {}
    try:
        payload = await updates._tracker_cached()
        if payload:
            matched, _ = updates._match_entries(payload)
            repos = {sid: f"{e[0].get('owner')}/{e[0].get('repo')}" for sid, e in matched.items()
                     if e and e[0].get("owner") and e[0].get("repo")}
    except Exception:
        pass
    return verdicts.payload((await board()).verdicts, repos)


# ── mock mode ────────────────────────────────────────────────────────────────

_MOCK = {
    "caddy": (verdicts.NEXT_UPGRADE, "Go stdlib net/http fixes; nothing reachable from the routes in use.",
              [("CVE-2026-95692", "High", "libssl3", "3.5.9-r0", "3.5.9-r1", False)]),
    "immich": (verdicts.WAITING, None,
               [("GHSA-imm2-4x8r-qq3p", "Critical", "immich", "v3.4.2", "3.4.3", False)]),
}


def _mock_counts() -> models.SecurityCounts:
    c = models.SecurityCounts()
    for group, _, _ in _MOCK.values():
        if group == verdicts.NEXT_UPGRADE:
            c.nextUpgrade += 1
        elif group == verdicts.WAITING:
            c.waiting += 1
    return c


def _mock_entry_fields() -> dict[str, dict]:
    return {sid: {"securityVerdict": g, "securityNote": note or f"{len(fs)} open finding, worst {fs[0][1].lower()}",
                  "securityColor": verdicts.COLORS[g], "securityCardUrl": None, "securityOpen": len(fs)}
            for sid, (g, note, fs) in _MOCK.items()}


def _mock_summary() -> models.SecuritySummary:
    rows = [models.SecurityServiceRow(id=sid, name=sid.title(), open=len(fs), worst=fs[0][1], kev=False,
                                      verdict=g, verdictNote=note, verdictColor=verdicts.COLORS[g])
            for sid, (g, note, fs) in _MOCK.items()]
    return models.SecuritySummary(scannedAt=mock._ago(hours=5), counts=_mock_counts(), services=rows, images=49)


def _mock_service(sid: str) -> models.ServiceSecurity:
    group, note, fs = _MOCK.get(sid, (None, None, []))
    return models.ServiceSecurity(
        id=sid, scannedAt=mock._ago(hours=5), tier="public" if sid == "caddy" else "tailnet",
        images=[models.SecurityImage(ref=f"{sid}:latest", digest=None, localBuild=False, packages=212,
                                     error=None, containers=[sid])],
        open=[models.SecurityFinding(id=i, aliases=[], severity=sev, package=p, version=ver, type="apk",
                                     fixedIn=[fix], kev=kev, epss=0.004, epssPercentile=0.39, url=None,
                                     summary="", image=f"{sid}:latest", why=f"{sev.lower()}, fix available")
              for i, sev, p, ver, fix, kev in fs],
        ignored=[], belowBar=37 if fs else 0,
        verdict=group, verdictNote=note, verdictColor=verdicts.COLORS[group] if group else None,
        reply=f"Verdict: {group}. {note}" if note else None, canAsk=bool(fs),
    )
