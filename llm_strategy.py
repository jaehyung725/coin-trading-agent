"""OpenAI chart analysis for the simulated strategy."""

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from strategies import ENTRY_PERCENTAGES, LEVERAGE_LEVELS, STRATEGIES, indicator_series
from operation_logs import operation_logger


LLM_STRATEGY = "llm"
AUTO_STRATEGIES = {**STRATEGIES, LLM_STRATEGY: "완전 자동 매매 (AI 에이전트)"}
ANALYSIS_INTERVAL = 5
DEFAULT_MODEL = "gpt-4.1-mini"
ACTIONS = ("long", "short", "close", "hold")


@dataclass(frozen=True)
class ModelDecision:
    action: str
    reason: str
    leverage: int = 0
    entry_percent: int = 0



def openai_settings():
    try:
        saved = st.secrets.get("openai", {})
    except StreamlitSecretNotFoundError:
        saved = {}
    key = (os.environ.get("OPENAI_API_KEY") or saved.get("api_key") or "").strip()
    if key == "YOUR_OPENAI_API_KEY":
        key = ""
    model = (os.environ.get("OPENAI_MODEL") or saved.get("model") or DEFAULT_MODEL).strip()
    return key, model or DEFAULT_MODEL


def candle_rows(candles):
    return [[c.start.isoformat(), c.opening, c.high, c.low, c.closing, c.volume] for c in candles]


def chart_decision(settings, minute, mark, position, load_candles):
    key, model = openai_settings()
    if not key:
        return ModelDecision("hold", "OpenAI API 키가 설정되지 않아 모델 주문을 대기합니다.")
    try:
        hour = minute.replace(minute=0)
        hourly = [c for c in load_candles(settings["market"], 60, hour, 100) if c.start < hour]
        minutes = [c for c in load_candles(settings["market"], 1, minute, 30) if c.start < minute]
        if (len(hourly) < 35 or hourly[-1].start != hour - timedelta(hours=1)
                or len(minutes) < 20 or minutes[-1].start != minute - timedelta(minutes=1)):
            return ModelDecision("hold", "마감된 차트 데이터가 부족해 모델 주문을 대기합니다.")
        indicators = ("볼린저 밴드", "SMA 20", "EMA 20", "RSI 14", "MACD")
        data = {
            "market": settings["market"],
            "mark": mark,
            "columns": ["utc", "open", "high", "low", "close", "volume"],
            "closed_hourly": candle_rows(hourly),
            "closed_minutes": candle_rows(minutes),
            "hourly_indicators": {label: float(values.iloc[-1]) for label, values in
                                  indicator_series(hourly, indicators, settings).items()},
            "minute_rsi14": float(indicator_series(minutes, ("RSI 14",), settings)["RSI 14"].iloc[-1]),
            "position": {"side": position.side, "entry": position.entry, "leverage": position.leverage}
                        if position else None,
        }
        return request_decision(key, model, data)
    except HTTPError as error:
        with error:
            operation_logger("worker").warning("Model or market HTTP failure status=%s", error.code)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        operation_logger("worker").warning("Model or market request failed type=%s", type(error).__name__)
    return ModelDecision("hold", "OpenAI 분석 또는 시세 확인에 실패해 모델 주문을 대기합니다.")


def request_decision(key, model, data):
    payload = {
        "model": model,
        "store": False,
        "instructions": (
            "Analyze the numeric market chart data for a simulated trading strategy. "
            "Consider trend, momentum, volatility and volume across both timeframes. "
            "For a flat account select long, short or hold. For an open position select close or hold. "
            "For long or short, choose leverage and the percentage of available cash to use for margin and entry fees. "
            "Scale both according to volatility, signal strength and agreement across timeframes. "
            "Avoid large exposure when evidence is uncertain. For hold or close, set leverage and entry_percent to 0. "
            "Use hold when evidence is mixed. Explain your decision and sizing briefly in Korean."
        ),
        "input": json.dumps(data, ensure_ascii=False, allow_nan=False),
        "max_output_tokens": 300,
        "text": {"format": {
            "type": "json_schema", "name": "paper_trade_decision", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "properties": {"action": {"type": "string", "enum": list(ACTIONS)},
                               "reason": {"type": "string"},
                               "leverage": {"type": "integer", "enum": [0, *LEVERAGE_LEVELS.values()]},
                               "entry_percent": {"type": "integer", "enum": [0, *ENTRY_PERCENTAGES]}},
                "required": ["action", "reason", "leverage", "entry_percent"],
            },
        }},
    }
    decision = json.loads(response_text(key, payload))
    reason = decision["reason"].strip() if isinstance(decision["reason"], str) else ""
    action = decision["action"]
    leverage = decision["leverage"]
    entry_percent = decision["entry_percent"]
    if type(leverage) is not int or type(entry_percent) is not int:
        raise ValueError("Model sizing must be integral")
    if action in ("long", "short"):
        sizing_valid = leverage in LEVERAGE_LEVELS.values() and entry_percent in ENTRY_PERCENTAGES
    else:
        sizing_valid = leverage == entry_percent == 0
    if action in ACTIONS and reason and sizing_valid:
        return ModelDecision(action, reason[:300], leverage, entry_percent)
    raise ValueError("Model did not return a valid decision")


def response_text(key, payload, *, timeout=8):
    request = Request(
        "https://api.openai.com/v1/responses", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if result.get("status") == "completed":
        for item in result["output"]:
            if item.get("type") != "message":
                continue
            for part in item["content"]:
                if part.get("type") == "output_text" and isinstance(part["text"], str) and part["text"].strip():
                    return part["text"].strip()
    raise ValueError("Model response did not contain completed text")
