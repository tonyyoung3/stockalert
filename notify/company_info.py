"""Company name, industry, and a short theme line for Slack alerts."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

import yfinance as yf


InfoLoader = Callable[[str], dict]


@dataclass
class CompanyProfile:
    ticker: str
    symbol: str
    name: str | None = None
    industry: str | None = None
    sector: str | None = None
    theme: str | None = None
    error: str | None = None


def to_symbol(ticker: str) -> str:
    text = (ticker or "").strip().upper()
    if text.endswith(".TW") or text.endswith(".TWO"):
        return text
    if text.isdigit():
        return f"{text}.TW"
    return text


def clip_text(text: str, max_len: int = 140) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    cut = cleaned[:max_len].rsplit(" ", 1)[0]
    return (cut or cleaned[:max_len]) + "…"


def profile_from_info(ticker: str, info: dict) -> CompanyProfile:
    symbol = to_symbol(ticker)
    name = info.get("shortName") or info.get("longName")
    industry = info.get("industry") or info.get("industryDisp")
    sector = info.get("sector") or info.get("sectorDisp")
    summary = info.get("longBusinessSummary")
    return CompanyProfile(
        ticker=ticker.split(".")[0] if ticker[:1].isdigit() else ticker,
        symbol=symbol,
        name=name,
        industry=industry,
        sector=sector,
        theme=clip_text(summary) if summary else None,
    )


def fetch_profile(ticker: str, info_loader: InfoLoader | None = None) -> CompanyProfile:
    symbol = to_symbol(ticker)
    loader = info_loader or (lambda s: yf.Ticker(s).info or {})
    try:
        info = loader(symbol)
        if not info:
            return CompanyProfile(ticker=ticker.split(".")[0], symbol=symbol, error="empty_info")
        return profile_from_info(ticker, info)
    except Exception as exc:  # noqa: BLE001 — Slack still sends the chart
        return CompanyProfile(ticker=ticker.split(".")[0], symbol=symbol, error=str(exc))


def fetch_profiles(
    tickers: list[str],
    info_loader: InfoLoader | None = None,
) -> dict[str, CompanyProfile]:
    """Fetch once per ticker. Failures become empty profiles so posting continues."""
    out: dict[str, CompanyProfile] = {}
    for ticker in dict.fromkeys(tickers):
        out[ticker] = fetch_profile(ticker, info_loader=info_loader)
    return out


def format_slack_caption(profile: CompanyProfile) -> str:
    title = f"標的: *{profile.ticker}*"
    if profile.name:
        title += f"  {profile.name}"
    lines = [title]
    industry = profile.industry or profile.sector
    if industry:
        lines.append(f"產業: {industry}")
    if profile.theme:
        lines.append(f"題材: {profile.theme}")
    return "\n".join(lines)


def format_digest(rows: list[tuple[CompanyProfile, str]]) -> str:
    """One line per hit, then a hint to @Cursor in this thread."""
    lines = [f"今日訊號 {len(rows)} 檔"]
    for profile, pattern_label in rows:
        parts = [profile.ticker]
        if profile.name:
            parts.append(profile.name)
        if profile.industry or profile.sector:
            parts.append(profile.industry or profile.sector)
        parts.append(pattern_label)
        lines.append("• " + " — ".join(parts))
    lines.append("想追問某檔題材，回這則並 @Cursor")
    return "\n".join(lines)


def maybe_enrich_themes(
    profiles: list[CompanyProfile],
    completer: Callable[[str], str] | None = None,
) -> None:
    """If OPENAI_API_KEY is set, replace theme lines with short Traditional Chinese 題材."""
    usable = [p for p in profiles if p.error is None]
    if not usable:
        return
    if completer is None and not os.environ.get("OPENAI_API_KEY"):
        return

    payload = [
        {
            "ticker": p.ticker,
            "name": p.name,
            "industry": p.industry,
            "sector": p.sector,
            "summary": p.theme,
        }
        for p in usable
    ]
    prompt = (
        "把下列公司收成一句繁體中文題材（約 20–40 字），只講產業與近期常被提到的題材，不要喊單。\n"
        "只回 JSON 物件，key 是 ticker，value 是字串。\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        raw = (completer or _openai_complete)(prompt)
        mapping = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] theme enrichment failed: {exc}")
        return

    if not isinstance(mapping, dict):
        return
    by_ticker = {p.ticker: p for p in usable}
    for key, value in mapping.items():
        profile = by_ticker.get(str(key))
        if profile and isinstance(value, str) and value.strip():
            profile.theme = clip_text(value.strip(), max_len=80)


def _openai_complete(prompt: str) -> str:
    api_key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("HARNESS_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You write concise Traditional Chinese stock-theme blurbs. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        parsed = json.loads(resp.read().decode("utf-8"))
    content = parsed["choices"][0]["message"]["content"]
    return content
