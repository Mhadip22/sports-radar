import json
import os
import requests
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))
MORNING_SCAN_HOUR = 9
STATE_FILE = "radar_data.json"

FEEDS = {
    "NBA": {"url": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", "mins": 48},
    "NCAA": {"url": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard", "mins": 40},
    "NBL": {"url": "https://site.api.espn.com/apis/site/v2/sports/basketball/nbl/scoreboard", "mins": 40},
    "ATP": {"url": "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard", "mins": 0},
}


def create_safe_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


HTTP = create_safe_session()


def get_tehran_now():
    return datetime.now(TEHRAN_TZ)


def load_data():
    today_str = get_tehran_now().strftime("%Y-%m-%d")
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") != today_str:
                data["date"] = today_str
                data["morning_sent"] = False
                data["watchlist"] = {}
                data["alerted_ids"] = []
                data["live_bets_today"] = 0
                data["max_live_bets"] = 1
                data["daily_pnl"] = 0.0
            return data
        except Exception as e:
            print(f"[JSON Load Warning]: {e}")

    return {
        "date": today_str,
        "morning_sent": False,
        "last_update_id": 0,
        "bankroll": {
            "initial": 83.29,
            "cash": 88.96,
            "locked": 7.00,
            "max_daily_loss": -10.00
        },
        "daily_pnl": 0.0,
        "live_bets_today": 0,
        "max_live_bets": 1,
        "excluded_teams": [
            "San Antonio Spurs", "LA Clippers",
            "Portland Trail Blazers", "Milwaukee Bucks",
            "Memphis Grizzlies", "Golden State Warriors"
        ],
        "watchlist": {},
        "alerted_ids": []
    }


def save_data(data):
    tmp_file = f"{STATE_FILE}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_file, STATE_FILE)


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("[Missing Secrets]: BOT_TOKEN یا CHAT_ID تنظیم نشده است.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = HTTP.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if r.status_code != 200:
            print(f"[Telegram API Error]: {r.text}")
    except Exception as e:
        print(f"[Telegram Connection Error]: {e}")


def get_favorite_info(comp):
    odds = comp.get("odds") or []
    if not odds or not isinstance(odds[0], dict):
        return None, 0.0
    details = odds[0].get("details", "")
    parts = details.split()
    if len(parts) == 2:
        try:
            abbr = parts[0]
            spread = float(parts[1])
            if -16.5 <= spread <= -6.0:
                return abbr, spread
        except ValueError:
            pass
    return None, 0.0


def extract_stats(competitor):
    raw = {}
    for s in competitor.get("statistics") or []:
        try:
            raw[s.get("name")] = float(s.get("displayValue", 0))
        except ValueError:
            raw[s.get("name")] = 0.0
    return {
        "fg": raw.get("fieldGoalPct", 0.0),
        "tp": raw.get("threePointFieldGoalPct", 0.0),
        "reb": raw.get("rebounds", 0.0),
        "tov": raw.get("turnovers", 0.0),
    }


def evaluate_ev_signal(fav_stats, dog_stats, deficit, spread, period, cash_balance):
    reb_diff = fav_stats["reb"] - dog_stats["reb"]
    tov_diff = dog_stats["tov"] - fav_stats["tov"]
    hot_three_fluke = dog_stats["tp"] >= 46.0 and fav_stats["tp"] <= 30.0
    cold_fg_regression = fav_stats["fg"] > 0 and fav_stats["fg"] <= 38.0 and reb_diff >= 2

    score = 0
    reasons = []

    if spread <= -10.5:
        score += 1
        reasons.append(f"مدعی سنگین ({spread})")

    if reb_diff >= 3:
        score += 2
        reasons.append(f"برتری ریباند (+{int(reb_diff)})")
    elif reb_diff <= -4:
        score -= 2
        reasons.append(f"ضعف ریباند ({int(reb_diff)})")

    if hot_three_fluke:
        score += 2
        reasons.append(f"سه‌امتیازی غیرعادی حریف ({dog_stats['tp']}٪)")
    if cold_fg_regression:
        score += 1
        reasons.append(f"افت موقت شوت مدعی ({fav_stats['fg']}٪)")

    if fav_stats["fg"] >= 48.0 and deficit >= 10:
        score -= 2
        reasons.append("هشدار ضعف دفاعی مدعی")

    if tov_diff < -3:
        score -= 2
        reasons.append("توپ‌لو‌دادن بالا")
    if period == 3 and deficit >= 11:
        score -= 1

    if score >= 4:
        verdict = "🟢 سیگنال قوی (+EV بالا)"
        stake = round(min(cash_balance * 0.06, 6.5), 2)
    elif score >= 2:
        verdict = "🟡 سیگنال تاییدشده (+EV متعادل)"
        stake = round(min(cash_balance * 0.045, 5.0), 2)
    else:
        verdict = "🔴 تله (رد شد)"
        stake = 0.0

    return verdict, score, " | ".join(reasons) if reasons else "آمار متعادل", stake


def is_valid_deficit_window(deficit, period, game_mins):
    if period < 1 or period > 3:
        return False
    if game_mins == 48:
        windows = {1: (8, 15), 2: (9, 16), 3: (8, 12)}
    else:
        windows = {1: (7, 12), 2: (8, 13), 3: (7, 10)}
    low, high = windows[period]
    return low <= deficit <= high


def discover_games(data):
    found_lines = []
    manual_items = {k: v for k, v in data.get("watchlist", {}).items() if k.startswith("MANUAL_")}
    data["watchlist"] = manual_items

    for league in ["NBA", "NCAA", "NBL"]:
        try:
            res = HTTP.get(FEEDS[league]["url"], timeout=10).json()
            for ev in res.get("events") or []:
                ev_id = f"{league}_{ev.get('id')}"
                comp = (ev.get("competitions") or [{}])[0]
                competitors = comp.get("competitors") or []
                names = [c.get("team", {}).get("displayName", "") for c in competitors]

                if any(ex.lower() in n.lower() for n in names for ex in data["excluded_teams"]):
                    continue

                fav_abbr, spread = get_favorite_info(comp)
                if fav_abbr:
                    title = ev.get("shortName", fav_abbr)
                    data["watchlist"][ev_id] = {
                        "league": league,
                        "title": title,
                        "fav_abbr": fav_abbr,
                        "spread": spread,
                        "mins": FEEDS[league]["mins"]
                    }
                    found_lines.append(f"🏀 *[{league}]* {title} (`{fav_abbr} {spread}`)")
        except Exception as e:
            print(f"[Discovery {league} Info]: {e}")

    try:
        res = HTTP.get(FEEDS["ATP"]["url"], timeout=10).json()
        for ev in res.get("events") or []:
            for grp in ev.get("groupings") or []:
                for comp in grp.get("competitions") or []:
                    c_id = f"ATP_{comp.get('id')}"
                    comps = comp.get("competitors") or []
                    if len(comps) == 2:
                        p1 = comps[0].get("athlete", {}).get("displayName", "P1")
                        p2 = comps[1].get("athlete", {}).get("displayName", "P2")
                        r1 = comps[0].get("curatedRank", {}).get("current", 999)
                        r2 = comps[1].get("curatedRank", {}).get("current", 999)
                        if (r1 <= 25 and r2 >= 55) or (r2 <= 25 and r1 >= 55):
                            fav_p = p1 if r1 < r2 else p2
                            if any(ex.lower() in fav_p.lower() for ex in data["excluded_teams"]):
                                continue
                            title = f"{p1} vs {p2}"
                            data["watchlist"][c_id] = {
                                "league": "ATP",
                                "title": title,
                                "fav_player": fav_p
                            }
                            found_lines.append(f"🎾 *[ATP]* {title} (مدعی: `{fav_p}`)")
    except Exception as e:
        print(f"[Discovery ATP Info]: {e}")

    for k, v in manual_items.items():
        found_lines.append(f"📌 *[دستی]* {v['title']}")

    return found_lines


def monitor_live(data):
    if data.get("daily_pnl", 0.0) <= data["bankroll"].get("max_daily_loss", -10.0):
        return
    if data["live_bets_today"] >= data["max_live_bets"]:
        return

    cash = data["bankroll"]["cash"]

    for league in ["NBA", "NCAA", "NBL"]:
        try:
            res = HTTP.get(FEEDS[league]["url"], timeout=10).json()
            for ev in res.get("events") or []:
                ev_id = f"{league}_{ev.get('id')}"
                if ev_id not in data["watchlist"]:
                    continue

                comp = (ev.get("competitions") or [{}])[0]
                status = comp.get("status") or {}
                if status.get("type", {}).get("state") != "in":
                    continue

                period = status.get("period", 0)
                detail = status.get("type", {}).get("shortDetail", "")
                alert_key = f"{ev_id}_P{period}"
                if alert_key in data["alerted_ids"]:
                    continue

                info = data["watchlist"][ev_id]
                fav_abbr = info["fav_abbr"]
                comps = comp.get("competitors") or []
                fav = next((c for c in comps if c.get("team", {}).get("abbreviation") == fav_abbr), None)
                dog = next((c for c in comps if c.get("team", {}).get("abbreviation") != fav_abbr), None)
                if not fav or not dog:
                    continue

                fav_score = int(fav.get("score", 0))
                dog_score = int(dog.get("score", 0))
                deficit = dog_score - fav_score

                if is_valid_deficit_window(deficit, period, info["mins"]):
                    fav_stats = extract_stats(fav)
                    dog_stats = extract_stats(dog)
                    verdict, q_score, note_text, stake = evaluate_ev_signal(
                        fav_stats, dog_stats, deficit, info["spread"], period, cash
                    )

                    data["alerted_ids"].append(alert_key)
                    if q_score < 2:
                        continue

                    is_break = "End" in detail or "Half" in detail
                    timing = "✅ استراحت بازی (زمان ثبت)" if is_break else "⏳ صبر تا تایم‌اوت / پایان کوارتر"

                    msg = (
                        f"🚨 *شکار لایو ({league})*\n"
                        f"بازی: *{info['title']}*\n"
                        f"نتیجه: `{dog_score} - {fav_score}` ({detail})\n"
                        f"وضعیت: *{fav_abbr}* (`{info['spread']}`) الان *{deficit} امتیاز* عقب است.\n"
                        f"⏱ زمان‌بندی: {timing}\n"
                        f"🧠 تشخیص: *{verdict}* (امتیاز: `{q_score}`)\n"
                        f"📊 تحلیل: {note_text}\n"
                        f"💰 حجم پیشنهادی ایمن: `{stake} USDT` (فقط اگر ML بت‌فا حداقل ۱.۸۵ است)"
                    )
                    send_telegram(msg)
        except Exception:
            pass

    try:
        res = HTTP.get(FEEDS["ATP"]["url"], timeout=10).json()
        for ev in res.get("events") or []:
            for grp in ev.get("groupings") or []:
                for comp in grp.get("competitions") or []:
                    c_id = f"ATP_{comp.get('id')}"
                    if c_id not in data["watchlist"] or c_id in data["alerted_ids"]:
                        continue
                    if comp.get("status", {}).get("type", {}).get("state") != "in":
                        continue

                    fav_p = data["watchlist"][c_id]["fav_player"]
                    for c in comp.get("competitors") or []:
                        name = c.get("athlete", {}).get("displayName", "")
                        lines = c.get("linescores") or []
                        if name == fav_p and len(lines) >= 2 and lines[0].get("winner") is False:
                            data["alerted_ids"].append(c_id)
                            stake = round(min(cash * 0.045, 5.0), 2)
                            msg = (
                                f"🎾 *شکار لایو تنیس (ATP)*\n"
                                f"بازی: *{data['watchlist'][c_id]['title']}*\n"
                                f"مدعی (*{fav_p}*) ست اول را باخت.\n"
                                f"💰 حجم پیشنهادی: `{stake} USDT` (اگر درصد سرویس اول بالای ۶۵٪ است)"
                            )
                            send_telegram(msg)
    except Exception:
        pass


def handle_telegram_commands(data):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": data["last_update_id"] + 1, "timeout": 2}
    try:
        res = HTTP.get(url, params=params, timeout=8).json()
        for upd in res.get("result") or []:
            data["last_update_id"] = upd["update_id"]
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue

            text = msg.get("text", "").strip()
            b = data["bankroll"]

            if text == "/status":
                total_equity = round(b["cash"] + b["locked"], 2)
                net_profit = round(total_equity - b["initial"], 2)
                sign = "+" if net_profit >= 0 else ""
                ex_list = ", ".join(data["excluded_teams"]) or "هیچ"
                reply = (
                    f"💼 *ترازنامه حساب و رادار هوشمند*\n"
                    f"• سرمایه اولیه: `{b['initial']} USDT`\n"
                    f"• موجودی نقد: `{b['cash']} USDT`\n"
                    f"• قفل‌شده در میکس: `{b['locked']} USDT`\n"
                    f"• ارزش کل حساب: `{total_equity} USDT` (`{sign}{net_profit}`)\n"
                    f"• سود/زیان امروز: `{data.get('daily_pnl', 0.0)} USDT`\n"
                    f"• سهمیه لایو امروز: `{data['live_bets_today']}/{data['max_live_bets']}`\n"
                    f"• بازی‌های تحت نظر: `{len(data['watchlist'])} بازی`\n"
                    f"• تیم‌های قفل‌شده: `{ex_list}`"
                )
                send_telegram(reply)

            elif text == "/scan":
                games = discover_games(data)
                if games:
                    send_telegram("🔍 *لیست بازی‌های ارزشمند (NBA / NCAA / NBL / ATP):*\n\n" + "\n".join(games[:22]))
                else:
                    send_telegram("🔍 در حال حاضر بازی جدیدی با شرایط سیف در فیدها فعال نیست.")

            elif text.startswith("/watch "):
                note = text.replace("/watch ", "").strip()
                if note:
                    m_id = f"MANUAL_{int(datetime.now().timestamp())}"
                    data["watchlist"][m_id] = {"league": "MANUAL", "title": note}
                    send_telegram(f"📌 به سبد پایش روزانه اضافه شد: `{note}`")

            elif text.startswith("/winlive "):
                try:
                    parts = text.split()
                    payout = float(parts[1])
                    stake = float(parts[2]) if len(parts) > 2 else 5.0
                    b["cash"] = round(b["cash"] + payout, 2)
                    data["daily_pnl"] = round(data.get("daily_pnl", 0.0) + (payout - stake), 2)
                    if data["max_live_bets"] < 2:
                        data["max_live_bets"] = 2
                    send_telegram(f"🎯 برد لایو `{payout} USDT` ثبت شد!\nموجودی نقد: `{b['cash']} USDT` | سهمیه لایو دوم امروز باز شد.")
                except Exception:
                    send_telegram("❌ فرمت اشتباه. نمونه: `/winlive 9.5`")

            elif text.startswith("/win "):
                try:
                    parts = text.split()
                    payout = float(parts[1])
                    stake = float(parts[2]) if len(parts) > 2 else 7.0
                    b["cash"] = round(b["cash"] + payout, 2)
                    b["locked"] = max(0.0, round(b["locked"] - stake, 2))
                    data["daily_pnl"] = round(data.get("daily_pnl", 0.0) + (payout - stake), 2)
                    send_telegram(f"🎉 برد میکس `{payout} USDT` ثبت شد!\nموجودی نقد جدید: `{b['cash']} USDT`")
                except Exception:
                    send_telegram("❌ فرمت اشتباه. نمونه: `/win 15.23`")

            elif text.startswith("/loss"):
                try:
                    parts = text.split()
                    stake = float(parts[1]) if len(parts) > 1 else 7.0
                    b["locked"] = max(0.0, round(b["locked"] - stake, 2))
                    data["daily_pnl"] = round(data.get("daily_pnl", 0.0) - stake, 2)
                    send_telegram(f"📉 باخت `{stake} USDT` تسویه شد. سود/زیان امروز: `{data['daily_pnl']} USDT`")
                except Exception:
                    send_telegram("❌ فرمت اشتباه. نمونه: `/loss 7`")

            elif text.startswith("/parlay "):
                try:
                    stake = float(text.split()[1])
                    if stake > b["cash"]:
                        send_telegram("⚠️ مبلغ میکس بیشتر از موجودی نقد است!")
                    else:
                        b["cash"] = round(b["cash"] - stake, 2)
                        b["locked"] = round(b["locked"] + stake, 2)
                        send_telegram(f"🎟 میکس `{stake} USDT` ثبت شد.\nنقد: `{b['cash']} USDT` | قفل‌شده: `{b['locked']} USDT`")
                except Exception:
                    send_telegram("❌ فرمت اشتباه. نمونه: `/parlay 7`")

            elif text.startswith("/bet "):
                try:
                    amt = float(text.split()[1])
                    if amt > b["cash"]:
                        send_telegram("⚠️ مبلغ شرط بیشتر از موجودی نقد است!")
                    else:
                        b["cash"] = round(b["cash"] - amt, 2)
                        data["live_bets_today"] += 1
                        send_telegram(f"✅ تک‌بازی لایو `{amt} USDT` ثبت شد.\nموجودی نقد جدید: `{b['cash']} USDT`")
                except Exception:
                    send_telegram("❌ فرمت اشتباه. نمونه: `/bet 5`")

            elif text.startswith("/setcash "):
                try:
                    exact_cash = float(text.split()[1])
                    b["cash"] = round(exact_cash, 2)
                    send_telegram(f"🔄 موجودی نقد با بت‌فا همگام شد: `{b['cash']} USDT`")
                except Exception:
                    send_telegram("❌ فرمت اشتباه. نمونه: `/setcash 88.96`")

            elif text.startswith("/exclude "):
                raw_teams = text.replace("/exclude ", "").split(",")
                added = []
                for t in raw_teams:
                    clean_t = t.strip()
                    if clean_t and clean_t not in data["excluded_teams"]:
                        data["excluded_teams"].append(clean_t)
                        added.append(clean_t)
                discover_games(data)
                send_telegram(f"🚫 تیم‌های قفل‌شده اضافه شدند: `{', '.join(added)}`")

            elif text == "/reset":
                data["excluded_teams"] = []
                data["live_bets_today"] = 0
                data["max_live_bets"] = 1
                data["daily_pnl"] = 0.0
                discover_games(data)
                send_telegram("♻️ لیست تیم‌های قفل‌شده و محدودیت روزانه ریست شد.")

            elif text == "/help":
                help_msg = (
                    "🛠 *راهنمای دستورهای ربات در تلگرام:*\n\n"
                    "• `/status` — ترازنامه حساب و سود خالص\n"
                    "• `/scan` — اسکن بازی‌های NBA, NCAA, NBL, ATP\n"
                    "• `/watch Anadolu Efes` — افزودن دستی بازی به لیست\n"
                    "• `/win 15.23` — ثبت برد میکس و واریز به نقد\n"
                    "• `/winlive 9.5` — ثبت برد لایو (باز کردن سهمیه دوم)\n"
                    "• `/loss 7` — تسویه برگ بازنده\n"
                    "• `/parlay 7` — ثبت میکس پیش‌بازی جدید\n"
                    "• `/bet 5` — ثبت تک‌بازی لایو\n"
                    "• `/setcash 88.96` — تنظیم مستقیم موجودی نقد\n"
                    "• `/exclude Efes, Partizan` — قفل کردن تیم‌های میکس\n"
                    "• `/reset` — شروع روز جدید"
                )
                send_telegram(help_msg)

    except Exception as e:
        print(f"[Command Error]: {e}")


if __name__ == "__main__":
    app_data = load_data()
    handle_telegram_commands(app_data)

    now = get_tehran_now()
    if not app_data["morning_sent"] and now.hour >= MORNING_SCAN_HOUR:
        games = discover_games(app_data)
        app_data["morning_sent"] = True
        if games:
            send_telegram("☀️ *گزارش خودکار بازی‌های ارزشمند امروز:*\n\n" + "\n".join(games[:22]))

    monitor_live(app_data)
    save_data(app_data)
    print(f"[{now.strftime('%H:%M')}] اسکن ابری با موفقیت انجام شد.")
