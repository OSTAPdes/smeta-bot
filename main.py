import os
import json
import uuid
import base64
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

import anthropic

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("supply-bot")

# --- Config from environment ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

BASE_DIR = Path(__file__).parent
RENDERS_DIR = BASE_DIR / "renders"
RENDERS_DIR.mkdir(exist_ok=True)
TEMPLATE = (BASE_DIR / "templates" / "app_template.html").read_text(encoding="utf-8")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_allowed(user_id: int) -> bool:
    # Если список пуст — бот закрыт для всех, пока явно не добавишь ID
    return user_id in ALLOWED_USER_IDS


def extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start_candidates = [i for i in [text.find("["), text.find("{")] if i != -1]
    if not start_candidates:
        raise ValueError("No JSON found in response")
    start = min(start_candidates)
    end = max(text.rfind("]"), text.rfind("}"))
    return json.loads(text[start:end + 1])


ANALYSIS_PROMPT = """Ты — ассистент архитектора-дизайнера. Перед тобой рендер интерьера или фасада, сделанный в 3ds Max.

Определи 8-14 ключевых позиций на изображении: материалы отделки (потолок, стены, пол), мебель, освещение, текстиль (шторы, подушки, пледы), декор (подсвечники, кашпо) — то, что реально можно закупить или повторить на украинском рынке.

Для каждой позиции укажи:
- id: порядковый номер начиная с 1
- title: короткое название позиции на русском
- eyebrow: категория одним словом ("Отделка", "Мебель", "Освещение", "Текстиль", "Декор")
- desc: 1-2 предложения с описанием — материал, цвет, фактура, примерные пропорции
- x, y: примерные координаты центра объекта на изображении в процентах от ширины и высоты (0-100)
- unit: "м²" для отделки стен/потолка/пола, "шт." для мебели/светильников/декора/текстиля, "компл." для штор
- tiered: true, если это материал-отделка из дерева/ДСП ИЛИ напольное/стеновое покрытие плиткой/камнем (для них нужен двухуровневый подбор: инженерный материал и натуральный); false для остального

Ответь СТРОГО в виде JSON-массива объектов с этими полями. Никакого текста до или после JSON, никакого markdown."""


def build_research_prompt(item: dict) -> str:
    if item.get("tiered"):
        guidance = (
            "Это материал-отделка. Сначала найди через веб-поиск 1-2 варианта ДСП/ЛДСП "
            "с КОНКРЕТНЫМ декором Kronospan или Egger (точное название и код декора), "
            "максимально похожих по цвету и текстуре на описание. Затем добавь отдельным "
            "уровнем 1 вариант натурального материала (дерево/камень/кварц) у производителя "
            "или мастерской, продающих в Украине. Если это плитка/керамогранит/камень — "
            "сначала керамогранит у производителей с продажей в Украине (например Cersanit, "
            "Plitkashop и подобные), затем натуральный камень или кварцевый агломерат."
        )
    else:
        guidance = (
            "Найди через веб-поиск 1-2 реальных товара с ценой на украинских площадках "
            "(Prom.ua, Rozetka, профильные магазины), максимально похожих по описанию."
        )
    return f"""Ты помогаешь архитектору найти, где купить материал или предмет в Украине.

Категория: {item.get('title')} ({item.get('eyebrow')})
Описание: {item.get('desc')}

{guidance}

Для каждого найденного варианта используй цены и поставщиков ТОЛЬКО из реальных результатов поиска — никогда не придумывай цену или магазин. Если что-то не нашлось — не включай эту позицию.

Ответь СТРОГО в виде JSON-объекта (без markdown, без пояснений) такой формы:
{{"tiers": [{{"name": "название уровня (можно пустую строку, если уровень один)", "options": [{{"name": "...", "supplier": "...", "price_label": "например '2050 ₴' или 'цена по запросу'", "price_uah": число_или_null, "avail": "в наличии / под заказ / уточнить", "url": "домен источника"}}]}}]}}

Если совсем ничего не нашлось — верни {{"tiers": []}}."""


def analyze_render(image_b64: str, media_type: str) -> list:
    msg = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": ANALYSIS_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return extract_json(text)


def research_item(item: dict) -> dict:
    prompt = build_research_prompt(item)
    resp = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1800,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return extract_json(text)
    except Exception:
        log.warning("Could not parse research JSON for item %s", item.get("title"))
        return {"tiers": []}


def render_app_page(render_id: str, image_b64: str, items: list):
    html = TEMPLATE.replace("__IMG_B64__", image_b64).replace(
        "__ITEMS_JSON__", json.dumps(items, ensure_ascii=False)
    )
    (RENDERS_DIR / f"{render_id}.html").write_text(html, encoding="utf-8")


# --- Telegram handlers ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        await message.answer(
            "Доступ к боту пока ограничен. Если тебе нужен доступ — обратись к администратору.\n"
            f"Твой Telegram ID: {message.from_user.id}"
        )
        return
    await message.answer(
        "Привет! Пришли фото рендера интерьера или фасада — разберу материалы и соберу "
        "интерактивную карту с вариантами покупки в Украине."
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    if not is_allowed(message.from_user.id):
        await message.answer(
            f"Доступ к боту пока ограничен. Твой Telegram ID: {message.from_user.id} — "
            "передай его администратору, чтобы получить доступ."
        )
        return

    status = await message.answer("Анализирую рендер — это займёт около минуты…")

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_io = await bot.download_file(file.file_path)
    img_b64 = base64.b64encode(file_io.read()).decode()

    try:
        items = await asyncio.to_thread(analyze_render, img_b64, "image/jpeg")
    except Exception as e:
        log.exception("analyze_render failed")
        await status.edit_text(f"Не получилось разобрать рендер: {e}")
        return

    await status.edit_text(f"Нашёл {len(items)} позиций, ищу варианты покупки по каждой…")

    async def research(it):
        it["data"] = await asyncio.to_thread(research_item, it)
        return it

    items = await asyncio.gather(*(research(it) for it in items))

    render_id = uuid.uuid4().hex[:10]
    render_app_page(render_id, img_b64, list(items))

    if not PUBLIC_URL:
        await message.answer(
            "Карта готова, но переменная окружения PUBLIC_URL ещё не настроена — "
            "добавь её (адрес твоего сервиса на Railway/Render) и пришли рендер ещё раз."
        )
        return

    url = f"{PUBLIC_URL}/app/{render_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть карту материалов", web_app=WebAppInfo(url=url))
    ]])
    await message.answer("Готово!", reply_markup=kb)


# --- FastAPI app (serves the Mini App pages + keeps the process alive on Railway/Render) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    polling_task = asyncio.create_task(dp.start_polling(bot))
    log.info("Bot polling started")
    yield
    polling_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health():
    return PlainTextResponse("OK")


@app.get("/app/{render_id}")
def get_render(render_id: str):
    path = RENDERS_DIR / f"{render_id}.html"
    if not path.exists():
        return HTMLResponse("<h1>Не найдено</h1>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"))
