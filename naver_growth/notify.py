"""알림: Slack 웹훅 / 텔레그램 봇. 둘 다 없으면 콘솔 출력만."""
from __future__ import annotations

import requests


def send(cfg, title: str, lines: list[str]) -> None:
    text = f"*{title}*\n" + "\n".join(f"• {l}" for l in lines) if lines else f"*{title}*"
    print("\n" + text.replace("*", ""))
    if cfg.demo:
        return
    s = cfg.secrets
    try:
        if s.get("SLACK_WEBHOOK_URL"):
            requests.post(s["SLACK_WEBHOOK_URL"], json={"text": text}, timeout=10)
        if s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID"):
            requests.post(f"https://api.telegram.org/bot{s['TELEGRAM_BOT_TOKEN']}/sendMessage",
                          json={"chat_id": s["TELEGRAM_CHAT_ID"], "text": text.replace("*", "")}, timeout=10)
    except requests.RequestException as e:
        print(f"[notify] 전송 실패: {e}")
