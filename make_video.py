import asyncio, glob, json, os, subprocess
from pathlib import Path

import arabic_reshaper
import edge_tts
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920                 # عمودي (Shorts)
VOICE = "ar-EG-SalmaNeural"       # أو ar-SA-HamedNeural / ar-EG-ShakirNeural
BG, FG, ACCENT = (15, 23, 42), (255, 255, 255), (250, 204, 21)
OUT = Path("build")
OUT.mkdir(exist_ok=True)


def find_font():
    for pattern in ("*NotoSansArabic*.ttf", "*NotoNaskhArabic*.ttf"):
        found = glob.glob(f"/usr/share/fonts/**/{pattern}", recursive=True)
        if found:
            return found[0]
    raise SystemExit("Arabic font not found (install fonts-noto-core)")


FONT_PATH = find_font()


def shape(text):
    return get_display(arabic_reshaper.reshape(text))


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if draw.textlength(shape(test), font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def make_slide(text, path, size=84, color=FG):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 24], fill=ACCENT)
    font = ImageFont.truetype(FONT_PATH, size)
    lines = wrap(d, text, font, W - 160)
    line_h = int(size * 1.7)
    y = (H - line_h * len(lines)) // 2
    for line in lines:
        s = shape(line)
        w = d.textlength(s, font=font)
        d.text(((W - w) / 2, y), s, font=font, fill=color)
        y += line_h
    img.save(path)


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


async def tts(text, path):
    await edge_tts.Communicate(text, VOICE, rate="+0%").save(str(path))


STYLES = [
    "سؤال مثير ثم الإجابة عنه",
    "قصة قصيرة مشوقة",
    "هل تعلم؟ حقائق متتالية",
    "مقارنة بين شيئين",
    "أرقام صادمة",
    "أسطورة شائعة ثم الحقيقة",
]


def generate(subject, style, past_titles):
    import anthropic

    prompt = f"""اكتب سيناريو فيديو قصير (حوالي 60 ثانية) بالعربية الفصحى المبسطة عن السيارات.
الموضوع: {subject}
الأسلوب: {style}
عناوين فيديوهات سابقة (لا تكررها ولا تكرر زاويتها): {past_titles[-30:]}
الشروط:
- 6 شرائح بالضبط، وكل شريحة جملتان قصيرتان على الأكثر، والمجموع بين 110 و140 كلمة.
- معلومات صحيحة ومعروفة فقط، ولا تذكر أرقامًا أو تواريخ إلا إذا كنت متأكدًا منها.
- الشريحة الأولى خطّاف يشد المشاهد، والأخيرة دعوة للاشتراك.
أرجع JSON فقط بدون أي نص آخر بهذا الشكل:
{{"title": "...", "description": "...", "tags": ["..."], "slides": ["..."]}}"""
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def get_topic():
    subjects = json.loads(Path("topics.json").read_text(encoding="utf-8"))
    hist_path = Path("history.json")
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
    n = len(history)
    subject = subjects[n % len(subjects)]
    # كل ما تخلص القائمة وترجع للأول، يتغير الأسلوب فيطلع الموضوع بشكل جديد
    style = STYLES[(n // len(subjects)) % len(STYLES)]
    if isinstance(subject, dict):  # سيناريو جاهز بدون ذكاء اصطناعي
        return subject, history
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is missing")
    return generate(subject, style, history), history


def main():
    topic, history = get_topic()

    segments = []
    for i, text in enumerate(topic["slides"]):
        png, mp3, mp4 = OUT / f"s{i}.png", OUT / f"s{i}.mp3", OUT / f"s{i}.mp4"
        make_slide(text, png)
        asyncio.run(tts(text, mp3))
        dur = duration(mp3) + 0.5
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", str(png), "-i", str(mp3),
             "-af", "apad", "-t", f"{dur:.2f}", "-r", "30",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-ar", "44100", "-ac", "2", str(mp4)],
            check=True, capture_output=True)
        segments.append(mp4)

    lst = OUT / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in segments))
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "output.mp4"], check=True, capture_output=True)

    meta = {
        "title": f"{topic['title']} #Shorts",
        "description": topic.get("description", topic["title"]),
        "tags": topic.get("tags", []),
    }
    Path("meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    history.append(topic["title"])
    Path("history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Video length:", round(duration("output.mp4"), 1), "sec")


if __name__ == "__main__":
    main()
