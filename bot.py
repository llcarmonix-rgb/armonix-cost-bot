import os, json, re, base64
from datetime import datetime
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import anthropic

# ── CONFIG ───────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY","")
DATA_FILE  = "expenses.json"
PHOTOS_DIR = Path("receipts")
PHOTOS_DIR.mkdir(exist_ok=True)

claude = anthropic.Anthropic(api_key=CLAUDE_KEY)

SYSTEM_EXTRACT = """Ти — помічник для обліку витрат компанії Армонікс (виробництво ящиків, Буча).

Отримуєш текст від користувача. Визнач тип запиту і поверни ТІЛЬКИ JSON без пояснень.

Якщо це витрата або встановлення бюджету — intent: "expense":
{
  "intent": "expense",
  "amount": число (сума в гривнях, з ПДВ якщо є),
  "description": "опис витрати",
  "category": "матеріали|фурнітура|логістика|зарплата|оренда|послуги|інше",
  "payment": "готівка|рахунок",
  "paid": true|false (true якщо вже оплачено, false якщо ще не оплачено / рахунок виставлено),
  "vat": true|false,
  "vat_credit": true|false,
  "supplier": "назва постачальника або null",
  "budget_set": null або {"cash": число, "account": число}
}

Правила:
- "готівка", "готівкою", "кеш", "рахуємо в готівку" → payment: "готівка"
- "рахунок", "карткою", "безготівка", "на рахунок" → payment: "рахунок"
- "з ПДВ" → vat: true; "без ПДВ" → vat: false
- "пдв не зараховуємо", "без кредиту", "пдв не наш" → vat_credit: false
- "не оплачено", "ще не платили", "рахунок виставлено", "зобов'язання" → paid: false
- Якщо не вказано — paid: true (за замовчуванням вважаємо оплаченим)
- Якщо vat: true і payment: "готівка" і не сказано про кредит → vat_credit: false
- Якщо vat: true і payment: "рахунок" і не сказано "не зараховуємо" → vat_credit: true

Якщо це запит на видалення — intent: "delete":
{
  "intent": "delete",
  "expense_id": число або null
}

Якщо це запит додати квитанцію до витрати — intent: "attach":
{
  "intent": "attach",
  "expense_id": число
}

Якщо незрозуміло → {"intent": "unknown"}
"""

SYSTEM_RECEIPT = """Ти — помічник для обліку витрат. Перед тобою фото квитанції або накладної.
Витягни і поверни ТІЛЬКИ JSON:
{
  "amount": число (загальна сума з ПДВ),
  "description": "що куплено (коротко)",
  "supplier": "назва магазину/постачальника",
  "vat_amount": число або null,
  "date": "дата з квитанції або null"
}"""

# ── DATA LAYER ────────────────────────────────────────────────────────────────
def load_data():
    if Path(DATA_FILE).exists():
        return json.loads(Path(DATA_FILE).read_text(encoding="utf-8"))
    return {"budgets": {"cash": 0, "account": 0}, "expenses": [], "next_id": 1}

def save_data(d):
    Path(DATA_FILE).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def add_expense(data, amount, description, category, payment, paid, vat, vat_credit, supplier, photo_path=None):
    vat_amount    = round(amount - amount / 1.2, 2) if vat else 0
    amount_no_vat = round(amount / 1.2, 2) if vat else amount
    exp = {
        "id":           data["next_id"],
        "date":         datetime.now().strftime("%d.%m.%Y %H:%M"),
        "amount":       amount,
        "amount_no_vat":amount_no_vat,
        "vat_amount":   vat_amount,
        "description":  description,
        "category":     category,
        "payment":      payment,
        "paid":         paid,
        "vat":          vat,
        "vat_credit":   vat_credit,
        "supplier":     supplier,
        "photo":        str(photo_path) if photo_path else None
    }
    data["expenses"].append(exp)
    data["next_id"] += 1
    return exp

def delete_expense(data, expense_id=None):
    if not data["expenses"]:
        return None
    if expense_id is None:
        exp = data["expenses"][-1]
    else:
        exp = next((e for e in data["expenses"] if e["id"] == expense_id), None)
    if exp:
        data["expenses"] = [e for e in data["expenses"] if e["id"] != exp["id"]]
        if exp.get("photo") and Path(exp["photo"]).exists():
            Path(exp["photo"]).unlink(missing_ok=True)
    return exp

def get_expense(data, expense_id):
    return next((e for e in data["expenses"] if e["id"] == expense_id), None)

# ── CLAUDE HELPERS ────────────────────────────────────────────────────────────
def parse_expense_text(text):
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=SYSTEM_EXTRACT,
        messages=[{"role":"user","content":text}]
    )
    try:
        raw = re.sub(r"```json|```","", resp.content[0].text.strip()).strip()
        return json.loads(raw)
    except:
        return None

def parse_receipt_image(image_bytes, media_type="image/jpeg"):
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=SYSTEM_RECEIPT,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":media_type,"data":b64}},
            {"type":"text","text":"Витягни дані з цієї квитанції"}
        ]}]
    )
    try:
        raw = re.sub(r"```json|```","", resp.content[0].text.strip()).strip()
        return json.loads(raw)
    except:
        return None

def format_money(n):
    return f"{n:,.0f}".replace(",","_").replace("_"," ") + " грн"

# ── BALANCE HELPERS ───────────────────────────────────────────────────────────
def get_spent(data, payment=None):
    # тільки оплачені
    exps = [e for e in data["expenses"] if e.get("paid", True)]
    if payment:
        exps = [e for e in exps if e["payment"] == payment]
    return sum(e["amount"] for e in exps)

def get_unpaid(data):
    return [e for e in data["expenses"] if not e.get("paid", True)]

def get_vat_credit(data):
    return sum(e["vat_amount"] for e in data["expenses"]
               if e.get("vat_credit", False) and e.get("paid", True))

def get_no_vat_cash(data):
    return sum(e["amount"] for e in data["expenses"]
               if not e.get("vat_credit", False) and e["payment"] == "готівка" and e.get("paid", True))

# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
def make_expense_keyboard(exp_id, paid):
    paid_btn = InlineKeyboardButton(
        "✅ Оплачено" if paid else "✅ Позначити оплаченим",
        callback_data=f"paid_{exp_id}"
    )
    unpaid_btn = InlineKeyboardButton(
        "⏳ Не оплачено" if not paid else "⏳ Позначити не оплаченим",
        callback_data=f"unpaid_{exp_id}"
    )
    attach_btn = InlineKeyboardButton("📎 Додати квитанцію", callback_data=f"attach_{exp_id}")
    delete_btn = InlineKeyboardButton("🗑 Видалити", callback_data=f"del_{exp_id}")
    return InlineKeyboardMarkup([
        [paid_btn, unpaid_btn],
        [attach_btn, delete_btn]
    ])

def make_list_keyboard(expenses, page=0, page_size=8):
    start = page * page_size
    chunk = expenses[start:start + page_size]
    rows = []
    for e in chunk:
        status = "✅" if e.get("paid", True) else "⏳"
        label = f"{status} #{e['id']} {e['date'][:5]} · {e['description'][:15]} · {format_money(e['amount'])}"
        rows.append([InlineKeyboardButton(label, callback_data=f"view_{e['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"list_{page-1}"))
    if start + page_size < len(expenses):
        nav.append(InlineKeyboardButton("▶️ Далі", callback_data=f"list_{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def expense_detail_text(exp):
    status = "✅ Оплачено" if exp.get("paid", True) else "⏳ Не оплачено"
    if exp.get("vat"):
        credit_str = "✅ зараховується" if exp.get("vat_credit") else "❌ не зараховується"
        vat_line = (
            f"  ↳ без ПДВ: {format_money(exp['amount_no_vat'])}\n"
            f"  ↳ ПДВ: {format_money(exp['vat_amount'])} ({credit_str})\n"
        )
    else:
        vat_line = "  ⚠️ без ПДВ\n"
    photo_line = "📎 є фото квитанції" if exp.get("photo") and Path(exp["photo"]).exists() else "📎 квитанція не додана"
    return (
        f"📄 *Витрата #{exp['id']}*  {status}\n"
        f"📅 {exp['date']}\n"
        f"💰 {format_money(exp['amount'])}\n"
        f"{vat_line}"
        f"🏷 {exp['description']} · {exp['category']}\n"
        f"💳 {'💵 готівка' if exp['payment']=='готівка' else '🏦 рахунок'}\n"
        f"{'🏪 ' + exp['supplier'] + chr(10) if exp['supplier'] else ''}"
        f"{photo_line}"
    )

# ── PENDING ATTACH STATE ──────────────────────────────────────────────────────
# Зберігаємо в пам'яті: user_id → expense_id який чекає на фото
pending_attach: dict[int, int] = {}

# ── HANDLERS ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаю! Я бот обліку витрат Армонікс.\n\n"
        "Надішліть витрату текстом або фото квитанції.\n\n"
        "Приклади:\n"
        "• `400 фарба готівка без ПДВ`\n"
        "• `12000 фанера рахунок з ПДВ Епіцентр`\n"
        "• `5000 послуги готівка з ПДВ пдв не зараховуємо`\n"
        "• `8000 оренда рахунок не оплачено`\n"
        "• `бюджет готівка 100000 рахунок 250000`\n\n"
        "Команди: /balance · /report · /vat · /list · /help",
        parse_mode="Markdown"
    )

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    cash_budget = d["budgets"]["cash"]
    acc_budget  = d["budgets"]["account"]
    cash_spent  = get_spent(d, "готівка")
    acc_spent   = get_spent(d, "рахунок")
    vat_credit  = get_vat_credit(d)
    cash_left   = cash_budget - cash_spent
    acc_left    = acc_budget  - acc_spent
    unpaid      = get_unpaid(d)
    unpaid_sum  = sum(e["amount"] for e in unpaid)

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
        f"📋 *ПДВ кредит:* {format_money(vat_credit)}\n"
    )
    if unpaid:
        msg += f"\n⏳ *Зобов'язання (не оплачено):* {format_money(unpaid_sum)}\n"
        for e in unpaid[:5]:
            msg += f"  • #{e['id']} {e['description']} — {format_money(e['amount'])}\n"
        if len(unpaid) > 5:
            msg += f"  _...ще {len(unpaid)-5} позицій (/list)_\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    exps = d["expenses"]
    if not exps:
        await update.message.reply_text("Поки немає жодної витрати.")
        return

    month = datetime.now().strftime("%m.%Y")
    paid_exps   = [e for e in exps if e.get("paid", True)]
    unpaid_exps = [e for e in exps if not e.get("paid", True)]

    by_cat = {}
    for e in paid_exps:
        cat = e["category"]
        by_cat.setdefault(cat, {"total":0,"vat":0,"count":0})
        by_cat[cat]["total"] += e["amount"]
        by_cat[cat]["vat"]   += e["vat_amount"] if e.get("vat_credit") else 0
        by_cat[cat]["count"] += 1

    lines = [f"📊 *Звіт · {month}*\n"]
    total_all = total_vat = 0
    for cat, v in sorted(by_cat.items(), key=lambda x: -x[1]["total"]):
        vat_str = f" · ПДВ {format_money(v['vat'])}" if v["vat"] else ""
        lines.append(f"▪️ {cat.capitalize()}: {format_money(v['total'])}{vat_str}")
        total_all += v["total"]
        total_vat += v["vat"]

    lines.append(f"\n💰 *Оплачено разом: {format_money(total_all)}*")
    lines.append(f"✅ ПДВ кредит: {format_money(total_vat)}")
    lines.append(f"\n💵 Готівка: {format_money(get_spent(d,'готівка'))}  🏦 Рахунок: {format_money(get_spent(d,'рахунок'))}")

    if unpaid_exps:
        unpaid_sum = sum(e["amount"] for e in unpaid_exps)
        lines.append(f"\n⏳ *Не оплачено: {format_money(unpaid_sum)}* ({len(unpaid_exps)} позицій)")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_vat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    credit = get_vat_credit(d)
    exps_credit   = [e for e in d["expenses"] if e.get("vat_credit") and e.get("paid", True)]
    exps_no_credit= [e for e in d["expenses"] if e.get("vat") and not e.get("vat_credit") and e.get("paid", True)]
    no_credit_sum = sum(e["vat_amount"] for e in exps_no_credit)
    msg = (
        f"📋 *Аналіз ПДВ*\n\n"
        f"✅ *ПДВ кредит (зараховано):* {format_money(credit)}\n"
        f"  З {len(exps_credit)} оплачених витрат\n\n"
        f"❌ *ПДВ не зараховано:* {format_money(no_credit_sum)}\n"
        f"  З {len(exps_no_credit)} витрат\n\n"
        f"⚠️ *Готівка без ПДВ кредиту:* {format_money(get_no_vat_cash(d))}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    exps = list(reversed(d["expenses"]))
    if not exps:
        await update.message.reply_text("Поки немає жодної витрати.")
        return
    kb = make_list_keyboard(exps, page=0)
    await update.message.reply_text(
        f"📋 *Всі витрати* ({len(exps)} записів)\n✅ оплачено · ⏳ не оплачено\nНатисніть на рядок для деталей:",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def cmd_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Вкажіть номер: /receipt 3")
        return
    try:
        exp_id = int(args[0])
    except:
        await update.message.reply_text("Невірний номер.")
        return
    d = load_data()
    exp = get_expense(d, exp_id)
    if not exp:
        await update.message.reply_text(f"Витрата #{exp_id} не знайдена.")
        return
    if exp.get("photo") and Path(exp["photo"]).exists():
        await update.message.reply_photo(
            photo=open(exp["photo"],"rb"),
            caption=f"📎 Квитанція #{exp_id} · {exp['description']} · {format_money(exp['amount'])}"
        )
    else:
        await update.message.reply_text(expense_detail_text(exp) + "\n_(Фото не додано)_",
                                        parse_mode="Markdown")

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    exp_id = None
    if args:
        try:
            exp_id = int(args[0].lstrip("#"))
        except:
            pass
    d = load_data()
    exp = delete_expense(d, exp_id)
    if not exp:
        await update.message.reply_text("Витрату не знайдено.")
        return
    save_data(d)
    await update.message.reply_text(
        f"🗑 *Витрату видалено*\n#{exp['id']} · {exp['description']} · {format_money(exp['amount'])}",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команди:*\n\n"
        "/balance — залишок + зобов'язання\n"
        "/report — витрати по категоріях\n"
        "/vat — аналіз ПДВ кредиту\n"
        "/list — всі витрати з деталями\n"
        "/receipt N — фото квитанції #N\n"
        "/delete N — видалити витрату #N\n\n"
        "💬 *Введення витрат:*\n"
        "`400 фарба готівка без ПДВ`\n"
        "`12000 фанера рахунок з ПДВ Епіцентр`\n"
        "`8000 оренда рахунок не оплачено`\n"
        "`пдв не зараховуємо` — при готівці з ПДВ\n\n"
        "📎 *Додати квитанцію до запису:*\n"
        "`додати квитанцію #5` → потім надішліть фото\n"
        "або надішліть фото з підписом `#5`\n\n"
        "🗑 *Видалення:* `видали #3` · `скасуй останню`\n\n"
        "💼 *Бюджет:* `бюджет готівка 100000 рахунок 250000`",
        parse_mode="Markdown"
    )

# ── CALLBACK HANDLER ──────────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_str = query.data
    user_id  = query.from_user.id

    if data_str.startswith("del_"):
        exp_id = int(data_str.split("_")[1])
        d = load_data()
        exp = delete_expense(d, exp_id)
        if exp:
            save_data(d)
            await query.edit_message_text(
                f"🗑 *Витрату видалено*\n#{exp['id']} · {exp['description']} · {format_money(exp['amount'])}",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Вже видалено або не знайдено.")

    elif data_str.startswith("paid_") or data_str.startswith("unpaid_"):
        parts  = data_str.split("_")
        exp_id = int(parts[1])
        new_paid = data_str.startswith("paid_")
        d = load_data()
        exp = get_expense(d, exp_id)
        if exp:
            exp["paid"] = new_paid
            save_data(d)
            status_str = "✅ Оплачено" if new_paid else "⏳ Не оплачено"
            await query.edit_message_text(
                expense_detail_text(exp),
                parse_mode="Markdown",
                reply_markup=make_expense_keyboard(exp_id, new_paid)
            )
        else:
            await query.edit_message_text("Витрату не знайдено.")

    elif data_str.startswith("attach_"):
        exp_id = int(data_str.split("_")[1])
        d = load_data()
        exp = get_expense(d, exp_id)
        if not exp:
            await query.answer("Витрату не знайдено.", show_alert=True)
            return
        pending_attach[user_id] = exp_id
        await query.message.reply_text(
            f"📎 Надішліть фото квитанції для витрати *#{exp_id}* · {exp['description']}",
            parse_mode="Markdown"
        )

    elif data_str.startswith("view_"):
        exp_id = int(data_str.split("_")[1])
        d = load_data()
        exp = get_expense(d, exp_id)
        if not exp:
            await query.edit_message_text("Витрату не знайдено.")
            return
        kb = make_expense_keyboard(exp_id, exp.get("paid", True))
        # Додаємо кнопку "До списку"
        kb.inline_keyboard.append([
            InlineKeyboardButton("◀️ До списку", callback_data="list_0")
        ])
        if exp.get("photo") and Path(exp["photo"]).exists():
            await query.edit_message_text(
                expense_detail_text(exp),
                parse_mode="Markdown", reply_markup=kb
            )
            await query.message.reply_photo(
                photo=open(exp["photo"],"rb"),
                caption=f"📎 Квитанція #{exp_id}"
            )
        else:
            await query.edit_message_text(
                expense_detail_text(exp),
                parse_mode="Markdown", reply_markup=kb
            )

    elif data_str.startswith("list_"):
        page = int(data_str.split("_")[1])
        d = load_data()
        exps = list(reversed(d["expenses"]))
        if not exps:
            await query.edit_message_text("Список порожній.")
            return
        kb = make_list_keyboard(exps, page=page)
        await query.edit_message_text(
            f"📋 *Всі витрати* ({len(exps)} записів)\n✅ оплачено · ⏳ не оплачено\nНатисніть на рядок для деталей:",
            parse_mode="Markdown",
            reply_markup=kb
        )

# ── TEXT HANDLER ──────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    user_id = update.message.from_user.id
    await update.message.chat.send_action("typing")

    parsed = parse_expense_text(text)
    if not parsed:
        await update.message.reply_text("Не зрозумів 🤔 Спробуйте: `400 фарба готівка без ПДВ`",
                                        parse_mode="Markdown")
        return

    intent = parsed.get("intent", "expense")

    # ── ВИДАЛЕННЯ ──
    if intent == "delete":
        exp_id = parsed.get("expense_id")
        d = load_data()
        exp = delete_expense(d, exp_id)
        if not exp:
            await update.message.reply_text("Витрату не знайдено або список порожній.")
            return
        save_data(d)
        await update.message.reply_text(
            f"🗑 *Витрату видалено*\n#{exp['id']} · {exp['description']} · {format_money(exp['amount'])}",
            parse_mode="Markdown"
        )
        return

    # ── ПРИКРІПИТИ КВИТАНЦІЮ ──
    if intent == "attach":
        exp_id = parsed.get("expense_id")
        d = load_data()
        exp = get_expense(d, exp_id)
        if not exp:
            await update.message.reply_text(f"Витрата #{exp_id} не знайдена.")
            return
        pending_attach[user_id] = exp_id
        await update.message.reply_text(
            f"📎 Надішліть фото квитанції для витрати *#{exp_id}* · {exp['description']}",
            parse_mode="Markdown"
        )
        return

    if intent == "unknown":
        await update.message.reply_text("Не зрозумів 🤔 Спробуйте: `400 фарба готівка без ПДВ`",
                                        parse_mode="Markdown")
        return

    d = load_data()

    # ── БЮДЖЕТ ──
    if parsed.get("budget_set"):
        bs = parsed["budget_set"]
        if bs.get("cash"):    d["budgets"]["cash"]    = bs["cash"]
        if bs.get("account"): d["budgets"]["account"] = bs["account"]
        save_data(d)
        await update.message.reply_text(
            f"✅ Бюджет встановлено\n"
            f"💵 Готівка: {format_money(d['budgets']['cash'])}\n"
            f"🏦 Рахунок: {format_money(d['budgets']['account'])}"
        )
        return

    if not parsed.get("amount"):
        await update.message.reply_text("Не знайшов суму 🤔 Вкажіть суму в гривнях.")
        return

    vat        = parsed.get("vat", False)
    vat_credit = parsed.get("vat_credit", False) if vat else False
    paid       = parsed.get("paid", True)

    exp = add_expense(
        d,
        amount      = parsed["amount"],
        description = parsed.get("description",""),
        category    = parsed.get("category","інше"),
        payment     = parsed.get("payment","готівка"),
        paid        = paid,
        vat         = vat,
        vat_credit  = vat_credit,
        supplier    = parsed.get("supplier")
    )
    save_data(d)

    payment = exp["payment"]
    status_str = "✅ Оплачено" if paid else "⏳ Не оплачено"

    if exp["vat"]:
        credit_str = "✅ зараховується" if exp["vat_credit"] else "❌ не зараховується"
        vat_line = (
            f"  ↳ без ПДВ: {format_money(exp['amount_no_vat'])}\n"
            f"  ↳ ПДВ {format_money(exp['vat_amount'])} ({credit_str})\n"
        )
    else:
        vat_line = "  ⚠️ без ПДВ\n"

    if paid:
        budget = d["budgets"]["cash"] if payment=="готівка" else d["budgets"]["account"]
        spent  = get_spent(d, payment)
        left   = budget - spent
        balance_line = f"\n{'💵' if payment=='готівка' else '🏦'} Залишок: *{format_money(left)}*"
    else:
        balance_line = f"\n⏳ *Зобов'язання — не впливає на баланс*"

    msg = (
        f"{'✅' if paid else '⏳'} *Записано #{exp['id']}*  {status_str}\n"
        f"💰 {format_money(exp['amount'])} · {'💵 готівка' if payment=='готівка' else '🏦 рахунок'}\n"
        f"{vat_line}"
        f"🏷 {exp['description']} · {exp['category']}\n"
        f"{'🏪 ' + exp['supplier'] + chr(10) if exp['supplier'] else ''}"
        f"{balance_line}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown",
                                    reply_markup=make_expense_keyboard(exp["id"], paid))

# ── PHOTO HANDLER ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    caption = (update.message.caption or "").strip()
    user_id = update.message.from_user.id
    await update.message.chat.send_action("typing")

    photo     = update.message.photo[-1]
    tg_file   = await photo.get_file()
    img_bytes = await tg_file.download_as_bytearray()

    # ── Прикріпити до існуючої витрати ──
    # Варіант 1: pending_attach (після команди "додати квитанцію #N")
    # Варіант 2: підпис містить #N
    attach_id = pending_attach.pop(user_id, None)
    if not attach_id:
        match = re.match(r'^#(\d+)', caption)
        if match:
            attach_id = int(match.group(1))

    if attach_id:
        d = load_data()
        exp = get_expense(d, attach_id)
        if not exp:
            await update.message.reply_text(f"Витрата #{attach_id} не знайдена.")
            return
        photo_path = PHOTOS_DIR / f"receipt_{attach_id}.jpg"
        photo_path.write_bytes(img_bytes)
        exp["photo"] = str(photo_path)
        save_data(d)
        await update.message.reply_text(
            f"📎 *Квитанцію прикріплено до витрати #{attach_id}*\n{exp['description']} · {format_money(exp['amount'])}",
            parse_mode="Markdown"
        )
        return

    # ── Видалення через підпис ──
    if caption and any(w in caption.lower() for w in ["видали","скасуй","видалити","прибери"]):
        match = re.search(r'#(\d+)', caption)
        exp_id = int(match.group(1)) if match else None
        d = load_data()
        exp = delete_expense(d, exp_id)
        if exp:
            save_data(d)
            await update.message.reply_text(
                f"🗑 *Витрату видалено*\n#{exp['id']} · {exp['description']} · {format_money(exp['amount'])}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("Витрату не знайдено.")
        return

    # ── Нова витрата з квитанції ──
    parsed = parse_receipt_image(bytes(img_bytes))
    if not parsed or not parsed.get("amount"):
        await update.message.reply_text("Не вдалось зчитати квитанцію 🤔 Спробуйте чіткіше фото.")
        return

    payment    = "рахунок"
    vat_credit = True
    if caption:
        cap_low = caption.lower()
        if any(w in cap_low for w in ["готівка","готівкою","кеш","готівку"]):
            payment = "готівка"
            vat_credit = False
        if any(w in cap_low for w in ["не зараховуємо","без кредиту","пдв не наш"]):
            vat_credit = False

    vat = parsed.get("vat_amount") is not None and parsed.get("vat_amount", 0) > 0

    d = load_data()
    photo_path = PHOTOS_DIR / f"receipt_{d['next_id']}.jpg"
    photo_path.write_bytes(img_bytes)

    exp = add_expense(
        d,
        amount      = parsed["amount"],
        description = parsed.get("description","Квитанція"),
        category    = "матеріали",
        payment     = payment,
        paid        = True,
        vat         = vat,
        vat_credit  = vat_credit if vat else False,
        supplier    = parsed.get("supplier"),
        photo_path  = photo_path
    )
    if parsed.get("vat_amount"):
        exp["vat_amount"]    = parsed["vat_amount"]
        exp["amount_no_vat"] = exp["amount"] - exp["vat_amount"]
    save_data(d)

    credit_str = ""
    if vat:
        credit_str = "\n  ↳ ПДВ кредит: " + ("✅ зараховується" if vat_credit else "❌ не зараховується")
    vat_str = f"\n  ↳ ПДВ: {format_money(parsed['vat_amount'])}{credit_str}" if parsed.get("vat_amount") else ""

    msg = (
        f"📎 *Квитанцію зчитано і збережено #{exp['id']}*\n"
        f"💰 {format_money(exp['amount'])}{vat_str}\n"
        f"🏷 {exp['description']}\n"
        f"{'🏪 ' + exp['supplier'] if exp['supplier'] else ''}\n"
        f"📅 {parsed.get('date','—')}\n"
        f"💳 {'💵 готівка' if payment=='готівка' else '🏦 рахунок'}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown",
                                    reply_markup=make_expense_keyboard(exp["id"], True))

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("vat",     cmd_vat))
    app.add_handler(CommandHandler("list",    cmd_list))
    app.add_handler(CommandHandler("receipt", cmd_receipt))
    app.add_handler(CommandHandler("delete",  cmd_delete))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Бот запущено ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
