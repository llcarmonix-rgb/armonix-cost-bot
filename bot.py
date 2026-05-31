import os, json, re, base64
from datetime import datetime
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import anthropic

# ── CONFIG ──────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")       # від @BotFather
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY","") # від anthropic.com
DATA_FILE  = "expenses.json"
PHOTOS_DIR = Path("receipts")
PHOTOS_DIR.mkdir(exist_ok=True)

claude = anthropic.Anthropic(api_key=CLAUDE_KEY)

SYSTEM_EXTRACT = """Ти — помічник для обліку витрат компанії Армонікс (виробництво ящиків, Буча).

Отримуєш текст від користувача. Витягни з нього дані витрати і поверни ТІЛЬКИ JSON без пояснень:

{
  "amount": число (сума в гривнях),
  "description": "опис витрати",
  "category": "матеріали|фурнітура|логістика|зарплата|оренда|послуги|інше",
  "payment": "готівка|рахунок",
  "vat": true|false,
  "supplier": "назва постачальника або null",
  "budget_set": null або {"cash": число, "account": число}
}

Якщо це встановлення бюджету (наприклад "бюджет готівка 100000 рахунок 250000") — заповни budget_set.
Якщо незрозуміло — amount: null."""

SYSTEM_RECEIPT = """Ти — помічник для обліку витрат. Перед тобою фото квитанції або накладної.
Витягни і поверни ТІЛЬКИ JSON:
{
  "amount": число,
  "description": "що куплено",
  "supplier": "назва магазину/постачальника",
  "vat_amount": число або null,
  "date": "дата з квитанції або null"
}"""

# ── DATA LAYER ───────────────────────────────────────────────────────────────
def load_data():
    if Path(DATA_FILE).exists():
        return json.loads(Path(DATA_FILE).read_text(encoding="utf-8"))
    return {"budgets": {"cash": 0, "account": 0},
            "expenses": [], "next_id": 1}

def save_data(d):
    Path(DATA_FILE).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def add_expense(data, amount, description, category, payment, vat, supplier, photo_path=None):
    exp = {
        "id": data["next_id"],
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "amount": amount,
        "amount_no_vat": round(amount / 1.2, 2) if vat else amount,
        "vat_amount": round(amount - amount / 1.2, 2) if vat else 0,
        "description": description,
        "category": category,
        "payment": payment,
        "vat": vat,
        "supplier": supplier,
        "photo": str(photo_path) if photo_path else None
    }
    data["expenses"].append(exp)
    data["next_id"] += 1
    return exp

# ── CLAUDE HELPERS ────────────────────────────────────────────────────────────
def parse_expense_text(text):
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=SYSTEM_EXTRACT,
        messages=[{"role":"user","content":text}]
    )
    try:
        raw = resp.content[0].text.strip()
        raw = re.sub(r"```json|```","",raw).strip()
        return json.loads(raw)
    except:
        return None

def parse_receipt_image(image_bytes, media_type="image/jpeg"):
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=SYSTEM_RECEIPT,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":media_type,"data":b64}},
            {"type":"text","text":"Витягни дані з цієї квитанції"}
        ]}]
    )
    try:
        raw = resp.content[0].text.strip()
        raw = re.sub(r"```json|```","",raw).strip()
        return json.loads(raw)
    except:
        return None

def format_money(n):
    return f"{n:,.0f}".replace(",","_").replace("_"," ") + " грн"

# ── BALANCE HELPERS ───────────────────────────────────────────────────────────
def get_spent(data, payment=None):
    exps = data["expenses"]
    if payment:
        exps = [e for e in exps if e["payment"] == payment]
    return sum(e["amount"] for e in exps)

def get_vat_credit(data):
    return sum(e["vat_amount"] for e in data["expenses"] if e["vat"])

def get_no_vat_cash(data):
    return sum(e["amount"] for e in data["expenses"]
               if not e["vat"] and e["payment"] == "готівка")

# ── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаю! Я бот обліку витрат Армонікс.\n\n"
        "Надішліть витрату текстом або фото квитанції.\n\n"
        "Приклади:\n"
        "• `400 фарба готівка без ПДВ`\n"
        "• `12000 фанера карткою з ПДВ Епіцентр`\n"
        "• `бюджет готівка 100000 рахунок 250000`\n\n"
        "Команди: /баланс · /звіт · /пдв · /допомога",
        parse_mode="Markdown"
    )

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    cash_budget  = d["budgets"]["cash"]
    acc_budget   = d["budgets"]["account"]
    cash_spent   = get_spent(d, "готівка")
    acc_spent    = get_spent(d, "рахунок")
    vat_credit   = get_vat_credit(d)
    no_vat_cash  = get_no_vat_cash(d)

    cash_left = cash_budget - cash_spent
    acc_left  = acc_budget  - acc_spent

    msg = (
        f"💼 *Баланс · {datetime.now().strftime('%d.%m.%Y')}*\n\n"
        f"💵 *ГОТІВКА*\n"
        f"  Бюджет: {format_money(cash_budget)}\n"
        f"  Витрачено: {format_money(cash_spent)}\n"
        f"  Залишок: {'✅ ' if cash_left>=0 else '❌ '}{format_money(cash_left)}\n\n"
        f"🏦 *РАХУНОК*\n"
        f"  Бюджет: {format_money(acc_budget)}\n"
        f"  Витрачено: {format_money(acc_spent)}\n"
        f"  Залишок: {'✅ ' if acc_left>=0 else '❌ '}{format_money(acc_left)}\n\n"
        f"📋 *ПДВ кредит накопичено:* {format_money(vat_credit)}\n"
        f"⚠️ Готівка без ПДВ: {format_money(no_vat_cash)}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    exps = d["expenses"]
    if not exps:
        await update.message.reply_text("Поки немає жодної витрати.")
        return

    month = datetime.now().strftime("%m.%Y")
    month_exps = [e for e in exps if e["date"].startswith("01") or True]  # all

    by_cat = {}
    for e in month_exps:
        cat = e["category"]
        by_cat.setdefault(cat, {"total":0,"vat":0,"count":0})
        by_cat[cat]["total"] += e["amount"]
        by_cat[cat]["vat"]   += e["vat_amount"]
        by_cat[cat]["count"] += 1

    lines = [f"📊 *Звіт · {month}* ({len(month_exps)} записів)\n"]
    total_all = 0
    total_vat = 0
    for cat, v in sorted(by_cat.items(), key=lambda x: -x[1]["total"]):
        vat_str = f" · ПДВ {format_money(v['vat'])}" if v["vat"] else ""
        lines.append(f"▪️ {cat.capitalize()}: {format_money(v['total'])}{vat_str}")
        total_all += v["total"]
        total_vat += v["vat"]

    lines.append(f"\n💰 *Разом: {format_money(total_all)}*")
    lines.append(f"✅ ПДВ кредит: {format_money(total_vat)}")
    cash_t = get_spent(d,"готівка")
    acc_t  = get_spent(d,"рахунок")
    lines.append(f"\n💵 Готівка: {format_money(cash_t)}  🏦 Рахунок: {format_money(acc_t)}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_vat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    credit = get_vat_credit(d)
    no_vat = get_no_vat_cash(d)
    exps_with_vat = [e for e in d["expenses"] if e["vat"]]
    msg = (
        f"📋 *Аналіз ПДВ*\n\n"
        f"✅ ПДВ кредит (накопичено): {format_money(credit)}\n"
        f"  З {len(exps_with_vat)} витрат з ПДВ\n\n"
        f"⚠️ Витрати без ПДВ (готівка): {format_money(no_vat)}\n"
        f"  _(ПДВ кредит по них не нараховується)_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Вкажіть номер: /квитанція 3")
        return
    try:
        exp_id = int(args[0])
    except:
        await update.message.reply_text("Невірний номер.")
        return
    d = load_data()
    exp = next((e for e in d["expenses"] if e["id"] == exp_id), None)
    if not exp:
        await update.message.reply_text(f"Витрата #{exp_id} не знайдена.")
        return
    if exp.get("photo") and Path(exp["photo"]).exists():
        await update.message.reply_photo(
            photo=open(exp["photo"],"rb"),
            caption=f"📎 Квитанція #{exp_id} · {exp['description']} · {format_money(exp['amount'])}"
        )
    else:
        await update.message.reply_text(
            f"📄 Витрата *#{exp_id}*\n"
            f"📅 {exp['date']}\n"
            f"💰 {format_money(exp['amount'])}\n"
            f"🏷 {exp['description']}\n"
            f"📁 {exp['category']}\n"
            f"💳 {exp['payment']} · {'з ПДВ' if exp['vat'] else 'без ПДВ'}\n"
            f"{'🏪 ' + exp['supplier'] if exp['supplier'] else ''}\n"
            f"_(Фото квитанції не додано)_",
            parse_mode="Markdown"
        )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команди:*\n\n"
        "/баланс — залишок готівки та рахунку\n"
        "/звіт — витрати по категоріях\n"
        "/пдв — аналіз ПДВ кредиту\n"
        "/квитанція N — повернути квитанцію #N\n"
        "/допомога — ця підказка\n\n"
        "💬 *Введення витрат (текстом):*\n"
        "`400 фарба готівка без ПДВ`\n"
        "`12000 фанера карткою з ПДВ Епіцентр`\n\n"
        "📸 *Квитанція:* просто надішліть фото\n\n"
        "💼 *Бюджет:*\n"
        "`бюджет готівка 100000 рахунок 250000`",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    await update.message.chat.send_action("typing")

    parsed = parse_expense_text(text)
    if not parsed:
        await update.message.reply_text("Не зрозумів 🤔 Спробуйте: `400 фарба готівка без ПДВ`",
                                        parse_mode="Markdown")
        return

    d = load_data()

    # Budget setting
    if parsed.get("budget_set"):
        bs = parsed["budget_set"]
        if bs.get("cash"):    d["budgets"]["cash"]    = bs["cash"]
        if bs.get("account"): d["budgets"]["account"] = bs["account"]
        save_data(d)
        msg = (
            f"✅ Бюджет встановлено\n"
            f"💵 Готівка: {format_money(d['budgets']['cash'])}\n"
            f"🏦 Рахунок: {format_money(d['budgets']['account'])}"
        )
        await update.message.reply_text(msg)
        return

    if not parsed.get("amount"):
        await update.message.reply_text("Не знайшов суму 🤔 Вкажіть суму в гривнях.")
        return

    exp = add_expense(
        d,
        amount      = parsed["amount"],
        description = parsed.get("description",""),
        category    = parsed.get("category","інше"),
        payment     = parsed.get("payment","готівка"),
        vat         = parsed.get("vat", False),
        supplier    = parsed.get("supplier")
    )
    save_data(d)

    # Remaining balance
    payment = exp["payment"]
    budget  = d["budgets"]["cash"] if payment=="готівка" else d["budgets"]["account"]
    spent   = get_spent(d, payment)
    left    = budget - spent

    vat_line = (
        f"  ↳ без ПДВ: {format_money(exp['amount_no_vat'])}\n"
        f"  ↳ ПДВ кредит: *{format_money(exp['vat_amount'])}*\n"
    ) if exp["vat"] else "  ⚠️ без ПДВ\n"

    msg = (
        f"✅ *Записано #{exp['id']}*\n"
        f"💰 {format_money(exp['amount'])} · {'💵 '+payment if payment=='готівка' else '🏦 '+payment}\n"
        f"{vat_line}"
        f"🏷 {exp['description']} · {exp['category']}\n"
        f"{'🏪 ' + exp['supplier'] + chr(10) if exp['supplier'] else ''}"
        f"\n{'💵' if payment=='готівка' else '🏦'} Залишок: *{format_money(left)}*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    img_bytes = await tg_file.download_as_bytearray()

    parsed = parse_receipt_image(bytes(img_bytes))
    if not parsed or not parsed.get("amount"):
        await update.message.reply_text("Не вдалось зчитати квитанцію 🤔 Спробуйте чіткіше фото.")
        return

    d = load_data()
    # Save photo
    photo_path = PHOTOS_DIR / f"receipt_{d['next_id']}.jpg"
    photo_path.write_bytes(img_bytes)

    exp = add_expense(
        d,
        amount      = parsed["amount"],
        description = parsed.get("description","Квитанція"),
        category    = "матеріали",
        payment     = "рахунок",
        vat         = parsed.get("vat_amount") is not None and parsed.get("vat_amount",0) > 0,
        supplier    = parsed.get("supplier"),
        photo_path  = photo_path
    )
    if parsed.get("vat_amount"):
        exp["vat_amount"] = parsed["vat_amount"]
        exp["amount_no_vat"] = exp["amount"] - exp["vat_amount"]
    save_data(d)

    vat_str = f"\n  ↳ ПДВ: {format_money(parsed['vat_amount'])}" if parsed.get("vat_amount") else ""
    msg = (
        f"📎 *Квитанцію зчитано і збережено #{exp['id']}*\n"
        f"💰 {format_money(exp['amount'])}{vat_str}\n"
        f"🏷 {exp['description']}\n"
        f"{'🏪 ' + exp['supplier'] if exp['supplier'] else ''}\n"
        f"📅 {parsed.get('date','—')}\n\n"
        f"_Квитанцію збережено. Отримати: /квитанція {exp['id']}_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("баланс",   cmd_balance))
    app.add_handler(CommandHandler("balance",  cmd_balance))
    app.add_handler(CommandHandler("звіт",     cmd_report))
    app.add_handler(CommandHandler("report",   cmd_report))
    app.add_handler(CommandHandler("пдв",      cmd_vat))
    app.add_handler(CommandHandler("vat",      cmd_vat))
    app.add_handler(CommandHandler("квитанція",cmd_receipt))
    app.add_handler(CommandHandler("receipt",  cmd_receipt))
    app.add_handler(CommandHandler("допомога", cmd_help))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Бот запущено ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
