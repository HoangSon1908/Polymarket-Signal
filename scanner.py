import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

# ======================== CONFIG & CONSTANTS ========================
st.set_page_config(
    page_title="Polymarket CS2 Signal Scanner",
    page_icon="🎮",
    layout="wide",
)

DEFAULT_CS2_WALLET = "0xc9bfc7986767173b4f1c3fc9b2c6b255fc7381c0"

DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
STATE_FILE = "scanner_state.json"
VN_TZ = timezone(timedelta(hours=7))

EVM_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

TEAM_ALIASES = {
    "vp": "virtus.pro",
    "baks": "baks esports",
    "nxs": "nexus",
    "ic.a": "inner circle academy",
    "ice": "inner circle esports",
    "ef": "eternal fire",
    "g1": "genone",
    "bca": "betclic apogee esports",
    "priv": "private",
    "ntr": "nuclear tigeres",
    "pha": "phantom",
    "all": "alliance",
    "bcg": "bc.game esports",
    "sng": "sangal",
    "g2.a": "g2 ares",
    "vit": "vitality",
    "prv": "parivision",
    "pv": "parivision",
    "pvision": "parivision",
    "fal": "team falcons",
    "tl": "liquid",
    "mglz": "themongolz",
    "lg": "luminosity",
    "lgc": "legacy",
    "fnc": "fnatic",
    "hero": "heroic",
    "imp": "imperial",
    "aur": "aurora gaming",
    "bb": "betboom team",
    "ts": "spirit",
    "team spirit": "spirit",
    "navi": "natus vincere",
    "ast": "astralis",
    "gls": "galorys",
    "bar": "barca",
    "den": "dendele cs",
    "100t": "100 thieves",
    "gre": "grêmio esports",
    "tdp": "turma do pagode",
    "fut": "fut esports",
    "sin": "sinners",
    "fly": "flyquest",
    "jjh": "jijiehao",
    "wc": "wildcard",
}


# ======================== PERSISTENCE HELPERS ========================
def short_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def load_persisted_ids(active_keys: set[str] | None = None) -> set[str]:
    saved: set[str] = set()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    saved.update(str(x) for x in data)
        except Exception:
            pass

    qp_raw = st.query_params.get("entered", "")
    if isinstance(qp_raw, list):
        qp_raw = ",".join(qp_raw)
    if qp_raw and active_keys:
        hashes = {h.strip() for h in str(qp_raw).split(",") if h.strip()}
        for k in active_keys:
            if short_hash(k) in hashes:
                saved.add(k)
    return saved


def persist_entered_ids(entered_ids: set[str], active_keys: set[str] | None = None) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(entered_ids), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    try:
        if entered_ids:
            target_keys = (entered_ids & active_keys) if active_keys else entered_ids
            hashes = sorted({short_hash(k) for k in target_keys})
            if hashes:
                st.query_params["entered"] = ",".join(hashes)
            elif "entered" in st.query_params:
                del st.query_params["entered"]
        else:
            if "entered" in st.query_params:
                del st.query_params["entered"]
    except Exception:
        pass


# ======================== API FETCHERS (DATA API V2 CURSOR-ONLY) ========================
def fetch_positions_by_status(
    wallet: str,
    status: str,
    max_pages: int = 25,
    page_limit: int = 500,
) -> list[dict]:
    """Kéo vị thế theo chuẩn Polymarket Data API v2 (Cursor-only, không dùng offset)."""
    results: list[dict] = []
    w_clean = wallet.lower().strip()
    params: dict = {
        "user": w_clean,
        "status": status,
        "limit": page_limit,
    }
    seen_cursors: set[str] = set()

    for _ in range(max_pages):
        resp = requests.get(f"{DATA_API_BASE}/v2/positions", params=params, timeout=15)
        if resp.status_code == 429:
            time.sleep(1.5)
            continue
        resp.raise_for_status()
        payload = resp.json()

        if isinstance(payload, list):
            results.extend(payload)
            break

        batch = payload.get("data") or []
        results.extend(batch)

        pagination = payload.get("pagination") or {}
        next_cursor = pagination.get("next_cursor") or payload.get("next_cursor")
        has_more = pagination.get("has_more", bool(next_cursor))

        if not next_cursor or not has_more or next_cursor in seen_cursors or not batch:
            break
        seen_cursors.add(next_cursor)
        params = {
            "user": w_clean,
            "status": status,
            "cursor": next_cursor,
        }

    return results


def _extract_gamma_market_meta(m: dict) -> dict:
    events = m.get("events") or []
    ev_title = ""
    ev_slug = ""
    ev_start = None
    if isinstance(events, list) and events:
        ev_title = str(events[0].get("title") or "").strip()
        ev_slug = str(events[0].get("slug") or "").strip()
        ev_start = events[0].get("startTime") or events[0].get("gameStartTime")
    if not ev_title:
        ev_title = str(m.get("question") or "").strip()
    if not ev_slug:
        ev_slug = str(m.get("slug") or "").strip()

    game_start = m.get("gameStartTime") or m.get("eventStartTime") or ev_start
    return {
        "event_title": ev_title,
        "event_slug": ev_slug,
        "question": str(m.get("question") or "").strip(),
        "game_start_time": game_start,
    }


def fetch_gamma_events_meta(
    condition_ids: list[str],
    token_ids: list[str],
    cache: dict[str, dict],
) -> dict[str, dict]:
    missing_cids = [c for c in dict.fromkeys(condition_ids) if c and c not in cache]
    for i in range(0, len(missing_cids), 40):
        chunk = missing_cids[i : i + 40]
        try:
            resp = requests.get(
                f"{GAMMA_API_BASE}/markets",
                params={"condition_ids": ",".join(chunk), "limit": 100},
                timeout=12,
            )
            if resp.ok:
                markets = resp.json()
                if isinstance(markets, list):
                    for m in markets:
                        cid = str(m.get("conditionId") or m.get("condition_id") or "").strip()
                        if cid:
                            cache[cid] = _extract_gamma_market_meta(m)
        except Exception:
            pass

    still_missing_tokens = []
    token_to_cid: dict[str, str] = {}
    for cid, tid in zip(condition_ids, token_ids):
        if cid and cid not in cache and tid:
            still_missing_tokens.append(tid)
            token_to_cid[tid] = cid

    for i in range(0, len(still_missing_tokens), 40):
        chunk = still_missing_tokens[i : i + 40]
        try:
            resp = requests.get(
                f"{GAMMA_API_BASE}/markets",
                params={"clob_token_ids": ",".join(chunk), "limit": 100},
                timeout=12,
            )
            if resp.ok:
                markets = resp.json()
                if isinstance(markets, list):
                    for m in markets:
                        cid = str(m.get("conditionId") or m.get("condition_id") or "").strip()
                        raw_tids = m.get("clobTokenIds") or []
                        if isinstance(raw_tids, str):
                            try:
                                raw_tids = json.loads(raw_tids)
                            except Exception:
                                raw_tids = []
                        if not cid:
                            for t in raw_tids:
                                if str(t) in token_to_cid:
                                    cid = token_to_cid[str(t)]
                                    break
                        if cid:
                            cache[cid] = _extract_gamma_market_meta(m)
        except Exception:
            pass

    return cache


def fetch_clob_books(token_ids: list[str]) -> dict[str, dict]:
    books_map: dict[str, dict] = {}
    unique_tids = [t for t in dict.fromkeys(token_ids) if t]
    for i in range(0, len(unique_tids), 100):
        chunk = unique_tids[i : i + 100]
        payload = [{"token_id": tid} for tid in chunk]
        try:
            resp = requests.post(f"{CLOB_API_BASE}/books", json=payload, timeout=15)
            if resp.ok:
                data = resp.json()
                if isinstance(data, list):
                    for book in data:
                        aid = str(book.get("asset_id") or book.get("token_id") or "").strip()
                        if aid:
                            books_map[aid] = book
        except Exception:
            pass
    return books_map


# ======================== PARSING & FILTER LOGIC ========================
def safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        f = float(val)
        return default if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return default


def to_epoch_seconds(raw_ts) -> float | None:
    if not raw_ts:
        return None
    try:
        if isinstance(raw_ts, (int, float)):
            ts = float(raw_ts)
            if ts > 1e12:
                ts /= 1000.0
            return ts if ts > 0 else None
        s = str(raw_ts).strip()
        if s.isdigit():
            ts = float(s)
            if ts > 1e12:
                ts /= 1000.0
            return ts if ts > 0 else None
        s_norm = re.sub(r"([+-]\d{2})$", r"\1:00", s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def parse_time_info(raw_ts) -> str:
    ts = to_epoch_seconds(raw_ts)
    if ts is None:
        return "—"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        age_mins = max(0.0, diff.total_seconds() / 60.0)
        mins = int(age_mins)
        if mins < 1:
            return "Vừa xong ⚡"
        if mins < 60:
            return f"{mins}p trước"
        hours = mins // 60
        rem_mins = mins % 60
        if hours < 24:
            return f"{hours}h{rem_mins:02d}p trước"
        days = hours // 24
        return f"{days}d {hours % 24}h trước"
    except Exception:
        return "—"


def format_start_countdown(start_ts: float | None, now_ts: float) -> tuple[bool, float | None, str]:
    """Trả về (is_live, secs_to_start, display_str)."""
    if start_ts is None:
        return False, None, "—"
    secs = start_ts - now_ts
    if secs <= 0:
        mins_ago = int(abs(secs) // 60)
        if mins_ago < 60:
            return True, secs, f"🔴 LIVE ({mins_ago}p)"
        return True, secs, f"🔴 LIVE ({mins_ago // 60}h{mins_ago % 60:02d}p)"
    mins = int(secs // 60)
    if mins < 60:
        return False, secs, f"⏳ {mins}p nữa"
    hours = mins // 60
    rem_mins = mins % 60
    if hours < 24:
        return False, secs, f"⏳ {hours}h{rem_mins:02d}p"
    days = hours // 24
    return False, secs, f"⏳ {days}d {hours % 24}h"


def calc_taker_breakeven_price(avg_p: float) -> float:
    """
    Tính giá Ask tối đa để lệnh Taker hòa vốn sau phí Sports 5% * p * (1 - p):
    p_whale / p_ask = 1.05 - 0.05 * p_ask  =>  p_ask = 10.5 - sqrt(110.25 - 20 * p_whale)
    """
    if avg_p <= 0.0:
        return 0.01
    disc = max(0.0, 110.25 - 20.0 * avg_p)
    p_be = 10.5 - math.sqrt(disc)
    return max(0.01, min(p_be, 0.99))


def normalize_team_name(name: str) -> str:
    clean_name = re.sub(r"\s+", " ", name).strip().lower()
    return TEAM_ALIASES.get(clean_name, clean_name)


def extract_team_pair(title: str) -> frozenset[str] | None:
    clean = re.sub(
        r"^(Counter-Strike|CS2)\s*:\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"^(Map\s*\d+\s*Rounds\s*Handicap|Map\s*\d+\s*Winner|Map\s*Handicap|Total\s*Maps|Match\s*Winner|Winner)\s*:\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\([^)]*\)", "", clean)
    clean = re.split(r"\s+-\s+|\s+\|\s+", clean)[0].strip()
    m = re.split(r"\s+vs\.?\s+", clean, maxsplit=1, flags=re.IGNORECASE)
    if len(m) == 2:
        t1 = normalize_team_name(m[0])
        t2 = normalize_team_name(m[1])
        if len(t1) >= 2 and len(t2) >= 2:
            return frozenset({t1, t2})
    return None


def matches_blocked_rule(
    combined_text: str,
    blocked_keywords: list[str],
) -> tuple[bool, str, bool]:
    """Trả về (is_blocked, reason, is_league_block)."""
    if not blocked_keywords:
        return False, "", False
    txt = combined_text.lower()
    is_cct_sa = "cct south america" in txt or "cct sa" in txt

    for kw in blocked_keywords:
        if kw == "cct europe":
            if not is_cct_sa and (
                re.search(r"\bcct\b.{0,28}\beurope\b", txt)
                or re.search(r"\bcct[\s\-_]*eu\b", txt)
                or "cct europe" in txt
            ):
                return True, "CCT Europe", True
        elif kw in ("bo1", "(bo1)"):
            if re.search(r"\(\s*bo1\s*\)|\bbo1\b", txt):
                return True, "Chặn BO1", False
        elif kw in ("bo5", "(bo5)"):
            if re.search(r"\(\s*bo5\s*\)|\bbo5\b", txt):
                return True, "Chặn BO5", False
        elif kw and kw in txt:
            return True, kw, True
    return False, "", False


def evaluate_cs2_positions(
    cfg: dict,
    open_positions: list[dict],
    redeem_positions: list[dict],
) -> dict:
    if "gamma_meta_cache" not in st.session_state:
        st.session_state["gamma_meta_cache"] = {}

    all_cids = [str(p.get("condition_id") or p.get("conditionId") or "").strip() for p in open_positions]
    all_tids = [str(p.get("token_id") or p.get("asset") or "").strip() for p in open_positions]
    gamma_cache = fetch_gamma_events_meta(all_cids, all_tids, st.session_state["gamma_meta_cache"])

    wallet = cfg["wallet"]
    multiplier = float(cfg["multiplier"])
    min_user_bet = float(cfg["min_user_bet"])
    max_user_cap = float(cfg["max_user_cap"])
    max_slippage_cents = float(cfg["max_slippage_cents"])
    drop_warn_cents = float(cfg["drop_warn_cents"])
    min_underdog_prob = int(cfg.get("min_underdog_prob", 15))
    max_fav_prob = int(cfg["max_fav_prob"])
    block_50_60 = bool(cfg["block_50_60"])
    detect_hedge = bool(cfg["detect_hedge"])
    block_live = bool(cfg.get("block_live", True))
    blocked_keywords = cfg["blocked_keywords"]
    now_ts = datetime.now(timezone.utc).timestamp()

    parsed_rows: list[dict] = []
    blocked_team_pairs: set[frozenset[str]] = set()
    seen_pos_keys: set[str] = set()

    for p in open_positions:
        cid = str(p.get("condition_id") or p.get("conditionId") or "").strip()
        tid = str(p.get("token_id") or p.get("asset") or "").strip()
        title = str(p.get("title") or p.get("question") or "Unknown Market").strip()
        outcome = str(p.get("outcome") or p.get("side") or "").strip()
        slug = str(p.get("slug") or p.get("event_slug") or "").strip()

        pos_key = f"{wallet[:10]}_{cid}_{tid}_{outcome}"
        if pos_key in seen_pos_keys:
            continue
        seen_pos_keys.add(pos_key)

        meta = gamma_cache.get(cid, {})
        ev_title = meta.get("event_title", "")
        ev_slug = meta.get("event_slug", "")
        if not slug and ev_slug:
            slug = ev_slug
        gamma_unverified = not bool(ev_title or ev_slug)

        start_ts = to_epoch_seconds(meta.get("game_start_time"))
        is_live, secs_to_start, start_countdown = format_start_countdown(start_ts, now_ts)

        avg_p = safe_float(p.get("avg_price") or p.get("avgPrice"), 0.0)
        cur_p = safe_float(p.get("current_price") or p.get("curPrice"), 0.0)
        cur_size = safe_float(p.get("current_size") or p.get("size"), 0.0)
        cur_val = safe_float(p.get("current_value") or p.get("currentValue"), 0.0)
        if cur_p <= 0 and cur_size > 0 and cur_val > 0:
            cur_p = cur_val / cur_size

        entry_cost = safe_float(
            p.get("entry_cost_usdc") or p.get("initial_value") or p.get("initialValue"),
            0.0,
        )
        if entry_cost <= 0 and cur_size > 0 and avg_p > 0:
            entry_cost = cur_size * avg_p

        realized_pnl = safe_float(p.get("realized_pnl") or p.get("realizedPnl"), 0.0)
        unrealized_pnl = safe_float(p.get("unrealized_pnl") or p.get("unrealizedPnl"), 0.0)
        if unrealized_pnl == 0.0 and cur_val > 0 and entry_cost > 0:
            unrealized_pnl = cur_val - entry_cost
        total_pnl = realized_pnl + unrealized_pnl

        last_ts = (
            p.get("last_event_at")
            or p.get("updated_at")
            or p.get("created_at")
            or p.get("timestamp")
        )
        last_ago_str = parse_time_info(last_ts)
        url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

        combined_text = f"{title} | {ev_title} | {slug} | {ev_slug}"
        is_blk, blk_reason, is_league_blk = matches_blocked_rule(combined_text, blocked_keywords)
        team_pair = extract_team_pair(title)
        if is_blk and is_league_blk and team_pair:
            blocked_team_pairs.add(team_pair)

        parsed_rows.append(
            {
                "pos_key": pos_key,
                "condition_id": cid,
                "token_id": tid,
                "title": title,
                "event_title": ev_title,
                "outcome": outcome,
                "slug": slug,
                "url": url,
                "avg_price": avg_p,
                "cur_price": cur_p,
                "entry_cost_usdc": entry_cost,
                "total_pnl": total_pnl,
                "last_buy_ago": last_ago_str,
                "is_live": is_live,
                "secs_to_start": secs_to_start,
                "start_countdown": start_countdown,
                "gamma_unverified": gamma_unverified,
                "is_blocked_direct": is_blk,
                "blocked_reason": blk_reason,
                "team_pair": team_pair,
            }
        )

    # Khóa chéo các kèo phụ (Map 1/2, Handicap...) cùng cặp đội thuộc giải chặn (vd: CCT Europe)
    for row in parsed_rows:
        if not row["is_blocked_direct"] and row["team_pair"] and row["team_pair"] in blocked_team_pairs:
            row["is_blocked_direct"] = True
            row["blocked_reason"] = "Khóa chéo cặp đội giải chặn"

    # Đếm số cửa đang mở trên cùng condition_id
    cid_outcomes: dict[str, set[str]] = {}
    for row in parsed_rows:
        if not row["is_blocked_direct"]:
            cid_outcomes.setdefault(row["condition_id"], set()).add(row["outcome"])

    # Gom danh sách token đạt toàn bộ quy tắc để kéo CLOB /books 1 lần duy nhất
    candidate_tids: list[str] = []
    for row in parsed_rows:
        row["cid_outcome_cnt"] = len(cid_outcomes.get(row["condition_id"], set()))
        if row["is_blocked_direct"]:
            continue
        if detect_hedge and row["cid_outcome_cnt"] >= 2:
            continue
        if not (0.02 < row["cur_price"] < 0.98):
            continue
        if block_live and row["is_live"]:
            continue
        raw_bet = row["entry_cost_usdc"] * multiplier
        if raw_bet < min_user_bet:
            continue
        avg_p = row["avg_price"]
        if avg_p <= 0.0 or avg_p < (min_underdog_prob / 100.0) or avg_p > (max_fav_prob / 100.0):
            continue
        if block_50_60 and (0.50 <= avg_p <= 0.60):
            continue
        candidate_tids.append(row["token_id"])

    books_map = fetch_clob_books(candidate_tids)

    valid_list: list[dict] = []
    slip_list: list[dict] = []
    hedge_list: list[dict] = []
    blocked_list: list[dict] = []
    rejected_list: list[dict] = []
    dead_zero_count = 0

    for row in parsed_rows:
        cur_p = row["cur_price"]
        avg_p = row["avg_price"]
        entry_cost = row["entry_cost_usdc"]
        raw_bet = entry_cost * multiplier
        final_bet = round(min(raw_bet, max_user_cap), 2)
        is_settled = cur_p >= 0.98 or cur_p <= 0.02

        # 1. Giải / Thể thức chặn (CCT Europe, BO1, BO5 + Khóa chéo giải đấu)
        if row["is_blocked_direct"]:
            cond_notes = []
            if is_settled:
                cond_notes.append(f"Đã ngã ngũ {cur_p * 100:.0f}¢")
            if raw_bet < min_user_bet:
                cond_notes.append(f"< Min ${min_user_bet:.2f}")
            if avg_p < (min_underdog_prob / 100.0):
                cond_notes.append(f"< {min_underdog_prob}%")
            elif block_50_60 and (0.50 <= avg_p <= 0.60):
                cond_notes.append("Dải 50-60%")
            elif avg_p > (max_fav_prob / 100.0):
                cond_notes.append(f"> {max_fav_prob}%")
            row["raw_user_bet"] = round(raw_bet, 2)
            row["other_cond"] = (
                f"Đạt vốn & giá ({row['blocked_reason']})"
                if not cond_notes
                else f"{row['blocked_reason']} | {', '.join(cond_notes)}"
            )
            blocked_list.append(row)
            continue

        # 2. Hedge 2 đầu trên cùng condition_id
        if detect_hedge and row["cid_outcome_cnt"] >= 2:
            row["raw_user_bet"] = round(raw_bet, 2)
            row["hedge_note"] = (
                "Ví gốc mở >= 2 cửa cùng trận (Đã ngã ngũ)"
                if is_settled
                else "Ví gốc đang mở >= 2 cửa cùng trận"
            )
            hedge_list.append(row)
            continue

        # 3. Vị thế đã ngã ngũ (0¢ hoặc 100¢)
        if is_settled:
            if cur_p <= 0.02:
                dead_zero_count += 1
            row["reject_reason"] = f"Đã ngã ngũ (giá {cur_p * 100:.1f}¢)"
            rejected_list.append(row)
            continue

        # 4. Chặn trận đã Live (qua gameStartTime)
        if block_live and row["is_live"]:
            row["reject_reason"] = f"Đã qua giờ bắt đầu ({row['start_countdown']})"
            rejected_list.append(row)
            continue

        # 5. Ngưỡng vốn tối thiểu
        if raw_bet < min_user_bet:
            min_whale_req = min_user_bet / multiplier if multiplier > 0 else 0
            row["reject_reason"] = (
                f"Vốn ví ${entry_cost:,.0f} (< ${min_whale_req:,.0f}) -> ${raw_bet:.2f} < Min ${min_user_bet:.2f}"
            )
            rejected_list.append(row)
            continue

        # 6. Bộ lọc dải xác suất
        if avg_p <= 0.0:
            row["reject_reason"] = "Giá mua không hợp lệ (0¢)"
            rejected_list.append(row)
            continue
        if avg_p < (min_underdog_prob / 100.0):
            row["reject_reason"] = f"Under quá sâu < {min_underdog_prob}% ({avg_p * 100:.1f}¢)"
            rejected_list.append(row)
            continue
        if avg_p > (max_fav_prob / 100.0):
            row["reject_reason"] = f"Kèo chắc ăn > {max_fav_prob}% ({avg_p * 100:.1f}¢)"
            rejected_list.append(row)
            continue
        if block_50_60 and (0.50 <= avg_p <= 0.60):
            row["reject_reason"] = f"Vùng cấm 50%–60% ({avg_p * 100:.1f}¢)"
            rejected_list.append(row)
            continue

        # 7. Đánh giá sổ lệnh CLOB, Hòa vốn Taker & Giá kê Limit tối ưu
        max_limit_price = min(avg_p + (max_slippage_cents / 100.0), 0.99)
        max_limit_cents = round(max_limit_price * 100.0, 1)

        taker_be_price = calc_taker_breakeven_price(avg_p)
        taker_be_cents = round(taker_be_price * 100.0, 1)

        book = books_map.get(row["token_id"], {})
        tick_size = safe_float(book.get("tick_size"), 0.01)
        if tick_size <= 0.0 or tick_size > 0.05:
            tick_size = 0.01

        asks_raw = book.get("asks") or []
        parsed_asks: list[tuple[float, float]] = []
        for a in asks_raw:
            ap = safe_float(a.get("price"), 0.0)
            asz = safe_float(a.get("size"), 0.0)
            if ap > 0 and asz > 0:
                parsed_asks.append((ap, asz))
        parsed_asks.sort(key=lambda x: x[0])

        notes: list[str] = []
        if row["gamma_unverified"]:
            notes.append("⚠️ Chưa xác minh Gamma")
        if row["secs_to_start"] is not None and 0 < row["secs_to_start"] <= 1800:
            notes.append("⏰ Sắp bắn (<30p)")

        if parsed_asks:
            best_ask = parsed_asks[0][0]
            avail_liq_usdc = sum(
                ap * asz for ap, asz in parsed_asks if ap <= max_limit_price + 1e-6
            )
            exec_price = best_ask
            is_empty_ask = False
        else:
            avail_liq_usdc = 0.0
            exec_price = cur_p if (0.02 < cur_p < 0.98) else avg_p
            is_empty_ask = True

        slippage_cents = round((exec_price - avg_p) * 100.0, 1)
        drop_cents = round((avg_p - exec_price) * 100.0, 1)
        is_slippage_over = (not is_empty_ask) and (slippage_cents > max_slippage_cents)

        # Tính giá kê Limit Maker tối ưu (đứng dưới Best Ask 1 tick để 0% phí, không vượt trần Maker)
        if not is_empty_ask and not is_slippage_over:
            rec_limit_price = max(0.01, min(round(exec_price - tick_size, 4), max_limit_price))
        else:
            rec_limit_price = max_limit_price
        rec_limit_cents = round(rec_limit_price * 100.0, 1)

        # Gắn nhãn 3 chế độ khớp lệnh
        if is_empty_ask:
            notes.insert(0, f"📋 TRỐNG ASK (Kê {rec_limit_cents:.1f}¢)")
        elif is_slippage_over:
            notes.insert(0, f"🟡 KÊ LIMIT CHỜ ({rec_limit_cents:.1f}¢)")
        elif exec_price <= taker_be_price + 1e-6:
            notes.insert(0, "⚡ TAKER OK")
        else:
            notes.insert(0, f"🎯 KÊ LIMIT ({rec_limit_cents:.1f}¢)")

        if drop_cents > drop_warn_cents:
            notes.append(f"⚠️ Tụt >{drop_warn_cents:.0f}¢")

        if avg_p < 0.30 and not is_empty_ask and slippage_cents > 1.5:
            notes.append("Under <30%: Kê <=+1.5¢")

        if not is_empty_ask and not is_slippage_over and avail_liq_usdc < final_bet:
            notes.append(f"💧 Ask mỏng (${avail_liq_usdc:.2f})")

        est_shares = final_bet / max(rec_limit_price, 0.01)
        needs_limit = is_empty_ask or is_slippage_over or (exec_price > taker_be_price + 1e-6)
        if needs_limit and est_shares < 5.0:
            min_limit_usdc = 5.0 * max(rec_limit_price, 0.01)
            notes.append(f"⚠️ Limit cần >=5sh (~${min_limit_usdc:.2f})")

        row["final_user_bet"] = final_bet
        row["exec_price"] = exec_price
        row["slippage_cents"] = slippage_cents
        row["taker_be_cents"] = taker_be_cents
        row["rec_limit_cents"] = rec_limit_cents
        row["max_limit_cents"] = max_limit_cents
        row["avail_liq_usdc"] = round(avail_liq_usdc, 2)
        row["action_note"] = " | ".join(notes)

        if is_slippage_over:
            slip_list.append(row)
        else:
            valid_list.append(row)

    return {
        "valid": pd.DataFrame(valid_list),
        "slip": pd.DataFrame(slip_list),
        "hedge": pd.DataFrame(hedge_list),
        "blocked": pd.DataFrame(blocked_list),
        "rejected": pd.DataFrame(rejected_list),
        "redeemable_count": len(redeem_positions),
        "dead_zero_count": dead_zero_count,
    }


# ======================== SESSION STATE INIT ========================
if "entered_ids" not in st.session_state:
    st.session_state["entered_ids"] = load_persisted_ids()
if "editor_ver" not in st.session_state:
    st.session_state["editor_ver"] = 0
if "scan_results" not in st.session_state:
    st.session_state["scan_results"] = None
if "last_scan_time" not in st.session_state:
    st.session_state["last_scan_time"] = None


# ======================== SIDEBAR CONFIG (CS2 ONLY) ========================
with st.sidebar:
    st.header("🎮 Cấu Hình Chiến Lược CS2")

    cs2_wallet_raw = st.text_input(
        "Địa chỉ ví CS2 Whale",
        value=DEFAULT_CS2_WALLET,
        key="w_cs2",
    ).strip()

    cs2_mult = st.number_input(
        "Hệ số nhân vốn",
        min_value=0.00001,
        max_value=1.0,
        value=0.00100,
        step=0.00010,
        format="%.5f",
        key="m_cs2",
    )
    c1, c2 = st.columns(2)
    cs2_min_bet = c1.number_input(
        "Min lệnh ($)",
        min_value=0.1,
        value=2.00,
        step=0.5,
        key="min_cs2",
    )
    cs2_max_cap = c2.number_input(
        "Trần tối đa ($)",
        min_value=1.0,
        value=20.00,
        step=1.0,
        key="max_cs2",
    )

    cs2_slip = st.slider("Trượt giá Maker tối đa (¢)", 0.5, 10.0, 2.5, 0.5, key="slip_cs2")
    cs2_drop_warn = st.slider("Cảnh báo giá tụt sâu (¢)", 2.0, 15.0, 5.0, 1.0, key="drop_cs2")
    cs2_min_prob = st.slider(
        "Sàn kèo cửa dưới tối thiểu (%)",
        5,
        45,
        15,
        5,
        key="min_prob_cs2",
        help="Chuẩn backtest: 15% (giữ dải 15–50% có kỳ vọng dương mạnh trên ví Whale).",
    )
    cs2_max_fav = st.slider("Chặn kèo chắc ăn trên (%)", 65, 99, 85, 5, key="fav_cs2")
    cs2_block_50_60 = st.checkbox("Chặn dải 50% – 60%", value=True, key="b5060_cs2")
    detect_hedge = st.checkbox("Chặn kèo ví gốc Hedge 2 đầu", value=True, key="hedge_cs2")
    block_live = st.checkbox(
        "Chặn kèo đã Live (qua gameStartTime)",
        value=True,
        key="live_cs2",
        help="Lấy giờ bắt đầu thực tế từ Gamma API để loại kèo đã vào trận.",
    )
    cs2_blocked_kw = st.text_input(
        "Giải / thể thức chặn (cách nhau dấu phẩy)",
        value="CCT Europe, BO1, BO5",
        key="blk_cs2",
    )

    cs2_min_whale = cs2_min_bet / cs2_mult if cs2_mult > 0 else 0
    cs2_max_whale = cs2_max_cap / cs2_mult if cs2_mult > 0 else 0
    st.caption(f"Ngưỡng vốn ví gốc: **${cs2_min_whale:,.0f} – ${cs2_max_whale:,.0f}**")

    st.divider()
    if st.session_state["entered_ids"]:
        if st.button(
            f"🧹 Xóa dấu Đã vào ({len(st.session_state['entered_ids'])})",
            use_container_width=True,
        ):
            st.session_state["entered_ids"] = set()
            st.session_state["editor_ver"] += 1
            persist_entered_ids(set(), active_keys=set())
            st.rerun()


# ======================== MAIN UI ========================
st.title("🎮 Trạm Quét Tín Hiệu Copy-Trade CS2")

col_btn, col_time = st.columns([2, 3])
scan_clicked = col_btn.button("🚀 QUÉT TÍN HIỆU CS2 NGAY", type="primary", use_container_width=True)

if scan_clicked or st.session_state["scan_results"] is None:
    if not EVM_ADDR_RE.match(cs2_wallet_raw):
        st.error("Địa chỉ ví CS2 chưa đúng định dạng 42 ký tự 0x...")
    else:
        with st.spinner("Đang kéo dữ liệu Polymarket Data API v2, Gamma & Sổ lệnh CLOB..."):
            cs2_cfg = {
                "wallet": cs2_wallet_raw.lower(),
                "multiplier": cs2_mult,
                "min_user_bet": cs2_min_bet,
                "max_user_cap": cs2_max_cap,
                "max_slippage_cents": cs2_slip,
                "drop_warn_cents": cs2_drop_warn,
                "min_underdog_prob": cs2_min_prob,
                "max_fav_prob": cs2_max_fav,
                "block_50_60": cs2_block_50_60,
                "detect_hedge": detect_hedge,
                "block_live": block_live,
                "blocked_keywords": [k.strip().lower() for k in cs2_blocked_kw.split(",") if k.strip()],
            }
            try:
                open_pos = fetch_positions_by_status(cs2_cfg["wallet"], "OPEN", max_pages=25, page_limit=500)
                redeem_pos = fetch_positions_by_status(cs2_cfg["wallet"], "REDEEMABLE", max_pages=1, page_limit=100)

                res = evaluate_cs2_positions(cs2_cfg, open_pos, redeem_pos)
                res["wallet"] = cs2_cfg["wallet"]
                st.session_state["scan_results"] = res
                st.session_state["last_scan_time"] = datetime.now(VN_TZ).strftime("%H:%M:%S %d/%m/%Y (GMT+7)")

                active_keys: set[str] = set()
                for k_df in ("valid", "slip"):
                    if not res[k_df].empty:
                        active_keys.update(res[k_df]["pos_key"].tolist())
                st.session_state["entered_ids"] = load_persisted_ids(active_keys)
            except Exception as exc:
                st.error(f"Lỗi kết nối Polymarket API: {exc}")

if st.session_state["last_scan_time"]:
    w_addr = (st.session_state["scan_results"] or {}).get("wallet", "")
    w_badge = f" • Ví: `{w_addr[:8]}...{w_addr[-4:]}`" if w_addr else ""
    col_time.info(f"🕒 Cập nhật: **{st.session_state['last_scan_time']}**{w_badge}")

res = st.session_state["scan_results"]
if res is not None:
    valid_df: pd.DataFrame = res["valid"].copy()
    slip_df: pd.DataFrame = res["slip"].copy()
    hedge_df: pd.DataFrame = res["hedge"].copy()
    blocked_df: pd.DataFrame = res["blocked"].copy()
    rejected_df: pd.DataFrame = res["rejected"].copy()
    redeem_cnt: int = res["redeemable_count"]
    dead_zero_cnt: int = res.get("dead_zero_count", 0)

    entered_set: set[str] = st.session_state["entered_ids"]
    active_keys_all: set[str] = set()
    if not valid_df.empty:
        active_keys_all.update(valid_df["pos_key"].tolist())
    if not slip_df.empty:
        active_keys_all.update(slip_df["pos_key"].tolist())

    remaining_capital = 0.0
    if not valid_df.empty:
        remaining_capital = float(
            valid_df.loc[~valid_df["pos_key"].isin(entered_set), "final_user_bet"].sum()
        )

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("🟢 Kèo hợp lệ ngay", len(valid_df))
    m2.metric("💰 Vốn cần thêm ($)", f"${remaining_capital:.2f}")
    m3.metric("🟡 Quá trượt giá (Limit)", len(slip_df))
    m4.metric("🚫 Giải / Thể thức chặn", len(blocked_df))
    m5.metric("🏆 Chờ Redeem / Xác 0¢", f"{redeem_cnt} / {dead_zero_cnt}")

    st.divider()

    # --- MỤC 1: KÈO HỢP LỆ ĐỂ VÀO LỆNH NGAY ---
    st.subheader(f"🟢 1. Kèo Hợp Lệ Để Vào Lệnh Ngay ({len(valid_df)})")
    if valid_df.empty:
        st.info("Hiện chưa có kèo CS2 nào khớp đủ quy tắc và nằm trong ngưỡng trượt giá Maker.")
    else:
        valid_df["entered"] = valid_df["pos_key"].isin(entered_set)
        disp_valid = valid_df[
            [
                "pos_key",
                "entered",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "taker_be_cents",
                "rec_limit_cents",
                "max_limit_cents",
                "avail_liq_usdc",
                "entry_cost_usdc",
                "start_countdown",
                "last_buy_ago",
                "url",
            ]
        ].copy()
        disp_valid["avg_price"] = (disp_valid["avg_price"] * 100.0).round(1)
        disp_valid["exec_price"] = (disp_valid["exec_price"] * 100.0).round(1)

        edited_valid = st.data_editor(
            disp_valid,
            key=f"valid_editor_{st.session_state['editor_ver']}",
            use_container_width=True,
            hide_index=True,
            column_order=[
                "entered",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "taker_be_cents",
                "rec_limit_cents",
                "max_limit_cents",
                "avail_liq_usdc",
                "entry_cost_usdc",
                "start_countdown",
                "last_buy_ago",
                "url",
            ],
            column_config={
                "entered": st.column_config.CheckboxColumn("✅ Đã vào", default=False),
                "title": st.column_config.TextColumn("Trận đấu / Kèo", width="medium"),
                "outcome": st.column_config.TextColumn("Cửa chọn", width="small"),
                "final_user_bet": st.column_config.NumberColumn("Tiền bạn vào ($)", format="$%.2f"),
                "action_note": st.column_config.TextColumn("Chế độ khớp lệnh", width="medium"),
                "avg_price": st.column_config.NumberColumn("Giá ví gốc (¢)", format="%.1f¢"),
                "exec_price": st.column_config.NumberColumn("Best Ask (¢)", format="%.1f¢"),
                "slippage_cents": st.column_config.NumberColumn("Trượt giá (¢)", format="%+.1f¢"),
                "taker_be_cents": st.column_config.NumberColumn(
                    "⚡ Hòa vốn Taker (¢)",
                    format="%.1f¢",
                    help="Best Ask <= mức này mới nên bấm mua ngay (đã trừ phí Sports 5%).",
                ),
                "rec_limit_cents": st.column_config.NumberColumn(
                    "🎯 Giá kê Limit (¢)",
                    format="%.1f¢",
                    help="Giá Bid tối ưu để đứng đầu sổ lệnh Maker (0% phí) mà không khớp Taker.",
                ),
                "max_limit_cents": st.column_config.NumberColumn(
                    "🛑 Trần Maker (¢)",
                    format="%.1f¢",
                    help="Giá tối đa cho phép kê lệnh Limit Maker.",
                ),
                "avail_liq_usdc": st.column_config.NumberColumn("Thanh khoản Ask ($)", format="$%.2f"),
                "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                "start_countdown": st.column_config.TextColumn("Giờ bắn", width="small"),
                "last_buy_ago": st.column_config.TextColumn("Cập nhật", width="small"),
                "url": st.column_config.LinkColumn("Polymarket", display_text="Mở kèo ↗"),
            },
            disabled=[
                "pos_key",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "taker_be_cents",
                "rec_limit_cents",
                "max_limit_cents",
                "avail_liq_usdc",
                "entry_cost_usdc",
                "start_countdown",
                "last_buy_ago",
                "url",
            ],
        )

        new_entered_valid = set(edited_valid.loc[edited_valid["entered"], "pos_key"].tolist())
        old_entered_valid = set(disp_valid.loc[disp_valid["entered"], "pos_key"].tolist())
        if new_entered_valid != old_entered_valid:
            updated_set = (entered_set - old_entered_valid) | new_entered_valid
            st.session_state["entered_ids"] = updated_set
            persist_entered_ids(updated_set, active_keys_all)
            st.rerun()

    # --- MỤC 2: KÈO ĐẠT ĐIỀU KIỆN NHƯNG QUÁ TRƯỢT GIÁ ASK ---
    if not slip_df.empty:
        st.subheader(f"🟡 2. Kèo Đạt Điều Kiện Nhưng Quá Trượt Giá Ask ({len(slip_df)})")
        slip_df["entered"] = slip_df["pos_key"].isin(st.session_state["entered_ids"])
        disp_slip = slip_df[
            [
                "pos_key",
                "entered",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "taker_be_cents",
                "rec_limit_cents",
                "max_limit_cents",
                "entry_cost_usdc",
                "start_countdown",
                "last_buy_ago",
                "url",
            ]
        ].copy()
        disp_slip["avg_price"] = (disp_slip["avg_price"] * 100.0).round(1)
        disp_slip["exec_price"] = (disp_slip["exec_price"] * 100.0).round(1)

        edited_slip = st.data_editor(
            disp_slip,
            key=f"slip_editor_{st.session_state['editor_ver']}",
            use_container_width=True,
            hide_index=True,
            column_order=[
                "entered",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "taker_be_cents",
                "rec_limit_cents",
                "max_limit_cents",
                "entry_cost_usdc",
                "start_countdown",
                "last_buy_ago",
                "url",
            ],
            column_config={
                "entered": st.column_config.CheckboxColumn("✅ Đã kê Limit", default=False),
                "title": st.column_config.TextColumn("Trận đấu / Kèo", width="medium"),
                "outcome": st.column_config.TextColumn("Cửa chọn", width="small"),
                "final_user_bet": st.column_config.NumberColumn("Tiền bạn vào ($)", format="$%.2f"),
                "action_note": st.column_config.TextColumn("Ghi chú", width="medium"),
                "avg_price": st.column_config.NumberColumn("Giá ví gốc (¢)", format="%.1f¢"),
                "exec_price": st.column_config.NumberColumn("Best Ask (¢)", format="%.1f¢"),
                "slippage_cents": st.column_config.NumberColumn("Trượt giá (¢)", format="%+.1f¢"),
                "taker_be_cents": st.column_config.NumberColumn("⚡ Hòa vốn Taker (¢)", format="%.1f¢"),
                "rec_limit_cents": st.column_config.NumberColumn("🎯 Giá kê Limit (¢)", format="%.1f¢"),
                "max_limit_cents": st.column_config.NumberColumn("🛑 Trần Maker (¢)", format="%.1f¢"),
                "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                "start_countdown": st.column_config.TextColumn("Giờ bắn", width="small"),
                "last_buy_ago": st.column_config.TextColumn("Cập nhật", width="small"),
                "url": st.column_config.LinkColumn("Polymarket", display_text="Mở kèo ↗"),
            },
            disabled=[
                "pos_key",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "taker_be_cents",
                "rec_limit_cents",
                "max_limit_cents",
                "entry_cost_usdc",
                "start_countdown",
                "last_buy_ago",
                "url",
            ],
        )

        new_entered_slip = set(edited_slip.loc[edited_slip["entered"], "pos_key"].tolist())
        old_entered_slip = set(disp_slip.loc[disp_slip["entered"], "pos_key"].tolist())
        if new_entered_slip != old_entered_slip:
            updated_set = (st.session_state["entered_ids"] - old_entered_slip) | new_entered_slip
            st.session_state["entered_ids"] = updated_set
            persist_entered_ids(updated_set, active_keys_all)
            st.rerun()

    # --- MỤC 3: CẢNH BÁO VÍ GỐC ĐANG HEDGE 2 ĐẦU ---
    if not hedge_df.empty:
        st.subheader(f"🟠 3. Cảnh Báo Ví Gốc Đang Hedge 2 Đầu ({len(hedge_df)})")
        disp_hedge = hedge_df[
            ["title", "outcome", "entry_cost_usdc", "raw_user_bet", "avg_price", "cur_price", "hedge_note", "url"]
        ].copy()
        disp_hedge["avg_price"] = (disp_hedge["avg_price"] * 100.0).round(1)
        disp_hedge["cur_price"] = (disp_hedge["cur_price"] * 100.0).round(1)
        st.dataframe(
            disp_hedge,
            use_container_width=True,
            hide_index=True,
            column_config={
                "title": "Trận đấu",
                "outcome": "Cửa",
                "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                "raw_user_bet": st.column_config.NumberColumn("Quy đổi ($)", format="$%.2f"),
                "avg_price": st.column_config.NumberColumn("Giá mua (¢)", format="%.1f¢"),
                "cur_price": st.column_config.NumberColumn("Giá hiện tại (¢)", format="%.1f¢"),
                "hedge_note": "Lý do chặn",
                "url": st.column_config.LinkColumn("Polymarket", display_text="Mở kèo ↗"),
            },
        )

    # --- MỤC 4: BẢNG THEO DÕI RIÊNG GIẢI / THỂ THỨC CHẶN ---
    if not blocked_df.empty:
        st.subheader(f"🚫 4. Bảng Theo Dõi Riêng: Giải / Thể Thức Chặn ({len(blocked_df)})")
        disp_blk = blocked_df[
            [
                "title",
                "outcome",
                "entry_cost_usdc",
                "raw_user_bet",
                "other_cond",
                "avg_price",
                "cur_price",
                "total_pnl",
                "url",
            ]
        ].copy()
        disp_blk["avg_price"] = (disp_blk["avg_price"] * 100.0).round(1)
        disp_blk["cur_price"] = (disp_blk["cur_price"] * 100.0).round(1)
        st.dataframe(
            disp_blk,
            use_container_width=True,
            hide_index=True,
            column_config={
                "title": "Trận đấu (Chặn)",
                "outcome": "Cửa",
                "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                "raw_user_bet": st.column_config.NumberColumn("Quy đổi ($)", format="$%.2f"),
                "other_cond": "Đánh giá điều kiện khác",
                "avg_price": st.column_config.NumberColumn("Giá ví gốc (¢)", format="%.1f¢"),
                "cur_price": st.column_config.NumberColumn("Giá hiện tại (¢)", format="%.1f¢"),
                "total_pnl": st.column_config.NumberColumn("PnL tạm tính ví gốc ($)", format="$%+,.2f"),
                "url": st.column_config.LinkColumn("Polymarket", display_text="Mở kèo ↗"),
            },
        )

    # --- MỤC 5: CÁC KÈO BỊ LOẠI BỞI BỘ LỌC ---
    if not rejected_df.empty:
        with st.expander(f"⚪ 5. Xem Danh Sách Kèo Đã Loại Bỏ ({len(rejected_df)})", expanded=False):
            disp_rej = rejected_df[
                ["title", "outcome", "entry_cost_usdc", "avg_price", "cur_price", "reject_reason"]
            ].copy()
            disp_rej["avg_price"] = (disp_rej["avg_price"] * 100.0).round(1)
            disp_rej["cur_price"] = (disp_rej["cur_price"] * 100.0).round(1)
            st.dataframe(
                disp_rej,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "title": "Trận đấu",
                    "outcome": "Cửa",
                    "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                    "avg_price": st.column_config.NumberColumn("Giá mua (¢)", format="%.1f¢"),
                    "cur_price": st.column_config.NumberColumn("Giá hiện tại (¢)", format="%.1f¢"),
                    "reject_reason": "Lý do loại",
                },
            )
