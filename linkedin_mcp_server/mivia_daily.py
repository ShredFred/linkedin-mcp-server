"""MiViA fork: daily collector for the LinkedIn report (runs without an MCP session).

    uv run --project <fork> python -m linkedin_mcp_server.mivia_daily --config cfg.json --out out.json

Reads, never writes to LinkedIn. Collects, per config:

* own posts (``/in/me/recent-activity/shares/``) and posts of administered
  company pages: engagers new since the last run, and post analytics;
* the fair radar: recent posts of the listed companies whose text carries one
  of the hashtags, with their commenters and reaction count;
* the event: attendee count every day, a full attendee scan only when the count
  grew by ``full_scan_growth`` or ``full_scan_days`` passed -- 31 search pages
  every day would spend the whole search budget;
* profile viewers new since the last run.

Each part runs on its own: a failure is recorded under ``errors`` and the rest
still runs. Exit codes follow the house rule: 0 ok, 3 ok with findings (errors
in some parts), 4 browser busy (another session holds the profile -- the caller
retries later), 5 login required, 1 failure.

Every page read asks the pacer first; a spent budget stops that part.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.scraping.mivia_engagement import (
    MiviaEngagementReader,
    SeenStore,
    engager_key,
)
from linkedin_mcp_server.scraping.mivia_actions import MiviaActions

logger = logging.getLogger("mivia_daily")

EXIT_OK, EXIT_FAIL, EXIT_FINDINGS, EXIT_BUSY, EXIT_LOGIN = 0, 1, 3, 4, 5

_URNS_JS = r"""(max) => {
  const out = [];
  for (const el of document.querySelectorAll('[data-urn^="urn:li:activity:"]')) {
    const urn = el.getAttribute('data-urn');
    if (out.some(o => o.urn === urn)) continue;
    // The post's own text sits in .update-components-text; the card's
    // innerText starts with labels and the author's headline (measured
    // 2026-09-29). Fall back to the card text only when that is missing.
    const body = el.querySelector('.update-components-text');
    const actor = el.querySelector('.update-components-actor__title, .update-components-actor__name');
    out.push({
      urn,
      text: ((body && body.innerText) || el.innerText || '').slice(0, 1500),
      actor: ((actor && actor.innerText) || '').split('\n')[0].trim(),
      has_body: !!body,
    });
    if (out.length >= max) break;
  }
  return out;
}"""

_EVENT_COUNT_JS = r"""() => {
  const t = (document.querySelector('main') || document.body).innerText || '';
  let m = /und\s+([\d.]+)\s+weitere\s+Person/i.exec(t);
  if (m) return parseInt(m[1].replace(/\./g, ''), 10) + 1;
  m = /and\s+([\d,]+)\s+other/i.exec(t);
  if (m) return parseInt(m[1].replace(/,/g, ''), 10) + 1;
  m = /([\d.,]+)\s+(Personen nehmen teil|attendees)/i.exec(t);
  return m ? parseInt(m[1].replace(/[.,]/g, ''), 10) : null;
}"""


class Busy(Exception):
    pass


class LoginRequired(Exception):
    pass


def activity_id(urn: str) -> str:
    return urn.rsplit(":", 1)[-1]


# Lines a post card renders before its text (accessibility labels, author,
# relative time, "promoted" markers), measured 2026-09-29.
_BOILERPLATE = re.compile(
    r"^(Nummer des Feedbeitrags|Feed-Beitrag|Feed post|Beitrag von|Gefällt|Follower|"
    r"Mehr anzeigen|Folgen|Follow|Übersetzung|Show translation|Boosten|Promoted|Gesponsert)"
    r"|hat das geteilt|reposted this|hat dies veröffentlicht"
    r"|^\d+\s*(Std\.|Tag|Woche|Monat|Jahr|h|d|w|mo|yr)\b",
    re.IGNORECASE,
)


def post_head(text: str) -> str:
    """First line of the post's own text, not of the card's labels."""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    for ln in lines:
        if len(ln) >= 25 and not _BOILERPLATE.search(ln):
            return ln[:80]
    return (lines[0] if lines else "")[:80]


def company_posts_url(slug: str) -> str:
    """The page's own posts as a member sees them. Without viewAsMember an admin
    is redirected to the admin dashboard, whose feed shows *other* pages' posts
    (measured 2026-09-29: Helmut Fischer, Georg Fischer instead of MiViA)."""
    return f"https://www.linkedin.com/company/{slug}/posts/?viewAsMember=true"


def has_hashtag(text: str, hashtags: list[str]) -> bool:
    low = (text or "").lower()
    return any(("#" + h.lower().lstrip("#")) in low for h in hashtags)


class Collector:
    def __init__(self, extractor: Any, config: dict[str, Any], state_dir: Path):
        self.cfg = config
        self.session = extractor._mivia_session
        self.navigator = extractor._mivia_navigator
        self.engagement = MiviaEngagementReader(self.session, self.navigator)
        self.actions = MiviaActions(self.session, self.navigator)
        self.pacer = outreach.Pacer(outreach.Ledger.default())
        self.seen = SeenStore()
        self.state_path = state_dir / "mivia-daily-state.json"
        self.errors: list[dict[str, str]] = []

    # -- helpers ---------------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(self.state_path)

    def _take(self, action: str, n: int = 1) -> None:
        self.pacer.take(action, n, tool="mivia_daily")

    async def _goto(self, url: str) -> None:
        self._take("page_read")
        await self.navigator._navigate_to_page(url)
        await self.session.check_rate_limit()
        await self.session.delay(random.uniform(3.0, 5.0))

    async def _urns(self, url: str, max_posts: int) -> list[dict[str, str]]:
        await self._goto(url)
        landed = str(getattr(self.session.page, "url", ""))
        if "/company/" in url and "/admin/" in landed:
            raise RuntimeError(
                f"redirected to the admin view ({landed}); refusing to read another feed"
            )
        for _ in range(3):
            items = await self.session.page.evaluate(_URNS_JS, max_posts)
            if len(items) >= max_posts:
                break
            await self.session.page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)"
            )
            await self.session.delay(random.uniform(1.8, 3.0))
        return await self.session.page.evaluate(_URNS_JS, max_posts)

    async def _part(self, name: str, coro: Any) -> Any:
        try:
            return await coro
        except outreach.PaceExceeded as spent:
            self.errors.append(
                {
                    "part": name,
                    "error": "pace_budget_spent",
                    "detail": json.dumps(spent.state),
                }
            )
        except (Busy, LoginRequired):
            raise
        except Exception as exc:  # one part must not stop the others
            name_ = type(exc).__name__
            if name_ in {"AuthenticationError"}:
                raise LoginRequired(str(exc)) from exc
            logger.exception("part %s failed", name)
            self.errors.append({"part": name, "error": name_, "detail": str(exc)[:300]})
        return None

    # -- parts -------------------------------------------------------------------

    async def _engagers(
        self, aid: str, *, reactors: bool, source: str
    ) -> dict[str, Any]:
        known = self.seen.keys(aid)
        out: dict[str, Any] = {
            "activity_id": aid,
            "source": source,
            "url": f"https://www.linkedin.com/feed/update/urn:li:activity:{aid}/",
        }
        found_r: list[dict[str, Any]] = []
        if reactors:
            self._take("page_read")
            r = await self.engagement.read_reactors(aid, 300)
            out["reactors_available"] = r["available"]
            found_r = r["reactors"]
            await self.session.delay(random.uniform(2.0, 4.0))
        self._take("page_read")
        page = await self.engagement.read_post_page(aid)
        out["reaction_count"] = page["reaction_count"]
        comments = page["comments"]

        def rk(x):
            return engager_key("reaction", x["id"], x["reaction"], name=x["name"])

        def ck(x):
            return engager_key(
                "comment", x["id"], x["comment_id"] or "", name=x["name"]
            )

        out["new_reactors"] = [x for x in found_r if rk(x) not in known]
        out["new_comments"] = [x for x in comments if ck(x) not in known]
        out["reactor_total"] = len(found_r)
        out["comment_total"] = len(comments)
        out["first_run"] = not known
        self.seen.remember(aid, {rk(x) for x in found_r} | {ck(x) for x in comments})
        return out

    async def own_posts(self) -> list[dict[str, Any]]:
        spec = self.cfg.get("own_posts") or {}
        if not spec.get("enabled", True):
            return []
        posts = []
        items = await self._part(
            "own_urns",
            self._urns(
                "https://www.linkedin.com/in/me/recent-activity/shares/",
                int(spec.get("max_posts", 5)),
            ),
        )
        for item in items or []:
            aid = activity_id(item["urn"])
            entry = await self._part(
                f"own_post:{aid}", self._engagers(aid, reactors=True, source="own")
            )
            if entry is not None:
                entry["text_head"] = post_head(item["text"])
                if spec.get("analytics", True):
                    self._take("page_read")
                    summary = await self._part(
                        f"analytics:{aid}", self.engagement.read_post_summary(aid)
                    )
                    entry["metrics"] = (summary or {}).get("metrics")
                posts.append(entry)
        for slug in spec.get("company_pages") or []:
            items = await self._part(
                f"page_urns:{slug}",
                self._urns(
                    company_posts_url(slug),
                    int(spec.get("max_posts", 5)),
                ),
            )
            for item in items or []:
                aid = activity_id(item["urn"])
                entry = await self._part(
                    f"page_post:{aid}",
                    self._engagers(aid, reactors=True, source=f"page:{slug}"),
                )
                if entry is not None:
                    entry["text_head"] = post_head(item["text"])
                    posts.append(entry)
        return posts

    async def radar(self) -> dict[str, Any]:
        spec = self.cfg.get("radar") or {}
        if not spec.get("enabled", False):
            return {"enabled": False}
        hashtags = spec.get("hashtags") or []
        out: dict[str, Any] = {"enabled": True, "posts": [], "companies_read": 0}
        for slug in spec.get("companies") or []:
            items = await self._part(
                f"radar_company:{slug}",
                self._urns(
                    company_posts_url(slug),
                    int(spec.get("max_posts_per_company", 5)),
                ),
            )
            if items is None:
                continue
            out["companies_read"] += 1
            for item in items:
                if hashtags and not has_hashtag(item["text"], hashtags):
                    continue
                aid = activity_id(item["urn"])
                entry = await self._part(
                    f"radar_post:{aid}",
                    self._engagers(aid, reactors=False, source=f"radar:{slug}"),
                )
                if entry is not None:
                    entry["text_head"] = post_head(item["text"])
                    out["posts"].append(entry)
        event_id = spec.get("event_id")
        if event_id:
            out["event"] = await self._part("event", self.event(str(event_id), spec))
        return out

    async def event(self, event_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        state = self._state()
        ev = (state.get("events") or {}).get(event_id) or {}
        await self._goto(f"https://www.linkedin.com/events/{event_id}/")
        count = await self.session.page.evaluate(_EVENT_COUNT_JS)
        result: dict[str, Any] = {
            "event_id": event_id,
            "attendee_count": count,
            "previous_count": ev.get("count"),
            "full_scan": False,
        }
        last_scan = ev.get("last_full_scan")
        days = (
            (datetime.now().astimezone() - datetime.fromisoformat(last_scan)).days
            if last_scan
            else None
        )
        grew = (
            count is not None
            and ev.get("scanned_count") is not None
            and count - ev["scanned_count"] >= int(spec.get("full_scan_growth", 10))
        )
        due = last_scan is None or grew or days >= int(spec.get("full_scan_days", 7))
        resume = int(ev.get("resume_page") or 0)
        if (due or resume) and count:
            total_pages = (count + 9) // 10
            start = resume or 1
            # Never more than the search budget leaves today, minus a reserve
            # for manual searches; the rest continues tomorrow from resume_page.
            left = self.pacer.state("search")["left"] - int(
                spec.get("search_reserve", 5)
            )
            pages = max(0, min(total_pages - start + 1, left))
            known = set(ev.get("attendees") or [])
            attendees: list[dict[str, Any]] = []
            page_no, last_read, finished = start, start - 1, False
            if pages:
                self._take("search", pages)
                end = start + pages - 1
                while page_no <= end:
                    chunk = await self.actions.get_event_attendees(
                        event_id, page_no, min(10, end - page_no + 1)
                    )
                    attendees += chunk["attendees"]
                    last_read = page_no + chunk["pages_read"] - 1
                    if chunk["complete"] or not chunk["next_page"]:
                        finished = True
                        break
                    page_no = chunk["next_page"]
                finished = finished or last_read >= total_pages
            new = [
                a
                for a in attendees
                if a.get("slug")
                and a["slug"] not in known
                and a.get("action") != "self"
            ]
            # Baseline only while nothing is known yet (a state from before this
            # field existed already holds its attendee list).
            baseline = not known and not ev.get("baseline_done")
            result.update(
                full_scan=True,
                scanned=len(attendees),
                pages=f"{start}-{last_read}/{total_pages}",
                new_attendees=[] if baseline else new,
                first_scan=baseline,
                scan_finished=finished,
            )
            ev["attendees"] = sorted(
                known | {a["slug"] for a in attendees if a.get("slug")}
            )
            if finished:
                ev.update(
                    resume_page=None,
                    baseline_done=True,
                    scanned_count=count,
                    last_full_scan=datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                )
            else:
                ev["resume_page"] = last_read + 1 if last_read >= start else start
        ev["count"] = count
        state.setdefault("events", {})[event_id] = ev
        self._save_state(state)
        return result

    async def viewers(self) -> dict[str, Any]:
        spec = self.cfg.get("profile_viewers") or {}
        if not spec.get("enabled", True):
            return {"enabled": False}
        self._take("page_read")
        res = await self.actions.profile_viewers(int(spec.get("limit", 50)))
        known = self.seen.keys("profile_viewers")
        keys = {
            engager_key("viewer", v["slug"], "", name=v["name"]) for v in res["viewers"]
        }
        new = [
            v
            for v in res["viewers"]
            if engager_key("viewer", v["slug"], "", name=v["name"]) not in known
        ]
        self.seen.remember("profile_viewers", keys)
        return {
            "enabled": True,
            "total_viewers": res["total_viewers"],
            "new_viewers": new if known else [],
            "first_run": not known,
        }

    async def run(self) -> dict[str, Any]:
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        report: dict[str, Any] = {"started_at": started}
        report["posts"] = await self._part("own_posts", self.own_posts()) or []
        report["radar"] = await self._part("radar", self.radar()) or {}
        report["viewers"] = await self._part("viewers", self.viewers()) or {}
        report["pace"] = self.pacer.summary()
        report["errors"] = self.errors
        report["finished_at"] = (
            datetime.now().astimezone().isoformat(timespec="seconds")
        )
        return report


async def _main(args: argparse.Namespace) -> int:
    from linkedin_mcp_server.drivers.browser import (
        close_browser,
        get_or_create_browser,
        set_headless,
    )
    from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    out_path = Path(args.out)
    state_dir = (
        Path(args.state_dir) if args.state_dir else Path.home() / ".linkedin-mcp"
    )
    status, code = "ok", EXIT_OK
    report: dict[str, Any] = {}
    set_headless(True)
    try:
        try:
            browser = await get_or_create_browser()
        except Exception as exc:
            if type(exc).__name__ == "BrowserBusyError":
                raise Busy(str(exc)) from exc
            if type(exc).__name__ == "AuthenticationError":
                raise LoginRequired(str(exc)) from exc
            raise
        try:
            collector = Collector(LinkedInExtractor(browser.page), config, state_dir)
            report = await collector.run()
        finally:
            await close_browser()
        if report.get("errors"):
            status, code = "partial", EXIT_FINDINGS
    except Busy as exc:
        status, code, report = "browser_busy", EXIT_BUSY, {"detail": str(exc)[:300]}
    except LoginRequired as exc:
        status, code, report = "login_required", EXIT_LOGIN, {"detail": str(exc)[:300]}
    except Exception as exc:
        logger.exception("collector failed")
        status, code, report = (
            "failed",
            EXIT_FAIL,
            {"detail": f"{type(exc).__name__}: {exc}"[:500]},
        )
    report = {"schema": "mivia_daily.v1", "status": status, **report}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_path)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--state-dir")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
