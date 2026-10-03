import asyncio, glob, json, os, random, re, subprocess
from pathlib import Path
from urllib.parse import quote
from google import genai
import arabic_reshaper
import edge_tts
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920                 # عمودي (Shorts)
VOICE = "ar-EG-SalmaNeural"       # أو ar-EG-ShakirNeural للصوت الرجالي
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



def make_caption(text, path, size=68):
    """طبقة شفافة فيها النص داخل مربع شبه معتم، تُوضع فوق مقطع الفيديو."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_PATH, size)
    lines = wrap(d, text, font, W - 200)
    line_h = int(size * 1.6)
    box_h = line_h * len(lines) + 70
    y0 = int(H * 0.60) - box_h // 2
    d.rounded_rectangle([60, y0, W - 60, y0 + box_h], radius=40, fill=(0, 0, 0, 170))
    y = y0 + 35
    for line in lines:
        s = shape(line)
        w = d.textlength(s, font=font)
        d.text(((W - w) / 2, y), s, font=font, fill=FG)
        y += line_h
    img.save(path)


PEXELS_URL = "https://api.pexels.com/videos/search"


SCENES = [  # (كلمات عربية، استعلام إنجليزي، قوة الزوم)
    (("محرك", "وانكل", "أسطوانة", "احتراق"), "car engine close up", 0.06),
    (("إطار", "إطارات", "عجل", "دولاب", "جنط"), "car wheel closeup", 0.06),
    (("فرامل", "مكابح", "الكبح"), "car brakes", 0.05),
    (("حزام", "وسادة", "مقصورة", "مقود"), "car interior dashboard", 0.04),
    (("كهربائي", "بطارية", "شحن"), "electric car charging", 0.04),
    (("سباق", "سباقات", "فورمولا", "لومان"), "race car track", 0.03),
    (("مصنع", "تصنيع", "خط تجميع"), "car factory assembly", 0.03),
    (("سرعة", "أسرع", "كيلومتر", "تسارع"), "fast car driving road", 0.03),
]


def scene_for(text, default_query):
    """يختار نوع اللقطة (واستعلام البحث وقوة الزوم) حسب كلام الشريحة."""
    for words, query, zoom in SCENES:
        if any(w in text for w in words):
            return query, zoom
    return default_query, 0.03


PIXABAY_URL = "https://pixabay.com/api/videos/"
SOURCES = set()


def pixabay_search(query):
    """بديل مجاني: مقاطع Pixabay (المفتاح مجاني من pixabay.com/api/docs)."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    import requests

    links = []
    try:
        r = requests.get(PIXABAY_URL,
                         params={"key": key, "q": query[:100], "per_page": 50,
                                 "safesearch": "true"},
                         timeout=30)
        for hit in r.json().get("hits", []):
            if hit.get("duration", 0) < 4:
                continue
            vs = [v for v in hit.get("videos", {}).values()
                  if v.get("url") and v.get("width", 0) >= 1280
                  and (v.get("size") or 0) <= 80_000_000]
            if not vs:
                continue
            vs.sort(key=lambda v: v["width"])
            good = [v for v in vs if v.get("height", 0) >= 1080]
            links.append((good[0] if good else vs[-1])["url"])
    except Exception as e:
        print("Pixabay error:", e)
    if links:
        SOURCES.add("Pixabay")
    return links


_cache = {}


def pexels_search(query):
    """روابط مقاطع عمودية مجانية من Pexels لاستعلام معيّن."""
    if query in _cache:
        return _cache[query]
    links = []
    key = os.environ.get("PEXELS_API_KEY")
    if not key and not os.environ.get("PIXABAY_API_KEY"):
        print("No PEXELS_API_KEY / PIXABAY_API_KEY: using text-only slides")
    if key:
        import requests

        try:
            r = requests.get(
                PEXELS_URL,
                params={"query": query, "orientation": "portrait",
                        "per_page": 30, "min_width": 720},
                headers={"Authorization": key}, timeout=30)
            for v in r.json().get("videos", []):
                if v.get("duration", 0) < 4:
                    continue
                files = [f for f in v.get("video_files", [])
                         if f.get("file_type") == "video/mp4" and f.get("width", 0) >= 720]
                if not files:
                    continue
                files.sort(key=lambda f: f["width"])
                good = [f for f in files if f["width"] >= 1000]
                links.append((good[0] if good else files[-1])["link"])
        except Exception as e:
            print("Pexels error:", e)
        if links:
            SOURCES.add("Pexels")
    if not links:
        links = pixabay_search(query)
    _cache[query] = links
    return links


def pick_link(query, fallback, used, rnd):
    for q in (query, fallback, "car"):
        cands = [l for l in pexels_search(q) if l not in used]
        if cands:
            link = rnd.choice(cands)
            used.add(link)
            return link
    return None


def download(url, path):
    import requests

    with requests.get(url, stream=True, timeout=120,
                      headers={"User-Agent": "yt-auto/1.0"}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


# --- WIKI START ---------------------------------------------------------
API = "https://ar.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "yt-auto/1.0 (educational video automation)"}


def wiki_extract(name):
    """يرجع (عنوان المقالة، مقدمتها كنص) أو None."""
    import requests

    base = {"action": "query", "format": "json", "prop": "extracts",
            "exintro": 1, "explaintext": 1}
    try:
        r = requests.get(API, params={**base, "redirects": 1, "titles": name},
                         headers=HEADERS, timeout=30)
        page = next(iter(r.json()["query"]["pages"].values()))
        if "missing" in page or not page.get("extract"):
            r = requests.get(API, params={**base, "generator": "search",
                                          "gsrsearch": name, "gsrlimit": 1},
                             headers=HEADERS, timeout=30)
            pages = r.json().get("query", {}).get("pages", {})
            if not pages:
                return None
            page = next(iter(pages.values()))
        if not page.get("extract"):
            return None
        return page["title"], page["extract"]
    except Exception as e:
        print("Wikipedia error for", name, ":", e)
        return None


def clean(text):
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def chunks(text, max_words=28):
    out = []
    for s in re.split(r"(?<=[.!؟?])\s+", text):
        words = s.split()
        if len(words) < 3:
            continue
        if len(words) <= max_words:
            out.append(s)
            continue
        cur = []
        for part in re.split(r"(?<=،)\s*", s):
            if not part:
                continue
            if cur and len((" ".join(cur) + " " + part).split()) > max_words:
                out.append(" ".join(cur))
                cur = []
            cur.append(part)
        if cur:
            out.append(" ".join(cur))
    return [" ".join(c.split()[:max_words + 12]) for c in out]


def build_slides(title, text):
    parts = chunks(clean(text))
    slides, cur, total = [], [], 0
    for p in parts:
        w = len(p.split())
        if total + w > 120:
            break
        cur.append(p)
        total += w
        if len(" ".join(cur).split()) >= 20:
            slides.append(" ".join(cur))
            cur = []
        if len(slides) == 5:
            break
    if cur and len(slides) < 5:
        slides.append(" ".join(cur))
    if total < 50:
        return None
    hook = f"معلومات سريعة عن {title}. تابع الفيديو حتى النهاية."
    outro = "شكرًا للمشاهدة! اشترك في القناة ليصلك المزيد من المعلومات الغريبة عن السيارات."
    return [hook] + slides + [outro]


def generate(subject, style, history):
    import time
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is missing")

    client = genai.Client(api_key=api_key)

    prompt = f"""
    أنت صانع محتوى سيارات وفيديوهات YouTube Shorts.
    الموضوع: سيارة {subject}
    الأسلوب: {style}

    المطلوب:
    اكتب سيناريو شورتس شيق من 5 إلى 7 جمل غنية بالمعلومات (مدة إلقاء صوتي بين 45 و55 ثانية).
    أرجع النتيجة بصيغة JSON حصراً بهذا الهيكل فقط دون أي نص إضافي:
    {{
        "slides": [
            "جملة افتتاحية خاطفة للانتباه",
            "معلومة قوية عن أداء المحرك والسرعة",
            "تفاصيل التصميم والهندسة المبتكرة",
            "ميزة فريدة أو تاريخية",
            "سؤال حماسي ومحفز للجمهور للتعليق"
        ]
    }}
    """

    for attempt in range(3):
        try:
            print(f"Calling gemini-3.8-flash (attempt {attempt + 1})...")
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=prompt,
                config={'response_mime_type': 'application/json'}
            )
            if response and response.text:
                return json.loads(response.text)
        except Exception as err:
            print(f"Server busy: {err}, waiting 5 seconds...")
            time.sleep(5)

    raise SystemExit("Gemini is currently overloaded. Please re-run later.")
def get_topic():
    subjects = json.loads(Path("topics.json").read_text(encoding="utf-8"))
    hist_path = Path("history.json")
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []

    styles = [
        "سرد حماسي وسريع مع التركيز على السرعة والتسارع",
        "أسلوب غامض ومشوق يبرز قوة المحرك وصوته المرعب",
        "أسلوب معلوماتي وثائقي فخم عن الفخامة والتاريخ"
    ]

    n = len(history)
    subject = subjects[n % len(subjects)]
    style = styles[(n // len(subjects)) % len(styles)]

    if isinstance(subject, dict):
        subj_name = subject.get("wiki") or subject.get("video") or subject.get("query")
    else:
        subj_name = subject

    topic_data = generate(subj_name, style, history)

    # ضبط كلمة البحث المطلوبة للـ video والـ query معاً
    search_term = f"{subj_name} sports car"
    if isinstance(subject, dict):
        search_term = subject.get("query") or subject.get("video") or search_term

    topic_data["query"] = search_term
    topic_data["video"] = search_term

    return topic_data, history
def main():
    topic, history = get_topic()

    rnd = random.Random(len(history))
    used = set()
    plan = []   # لكل شريحة: (مسار المقطع أو None، قوة الزوم)
    last = len(topic["slides"]) - 1
    for i, text in enumerate(topic["slides"]):
        if i == last:
            query, zoom = topic["query"], 0.03
        else:
            query, zoom = scene_for(text, topic["query"])
        link = pick_link(query, topic["query"], used, rnd)
        path = None
        if link:
            path = OUT / f"clip{i}.mp4"
            try:
                download(link, path)
            except Exception as e:
                print("Clip download failed:", e)
                path = None
        print(f"slide {i}: scene='{query}' clip={'yes' if path else 'no'}")
        plan.append((path, zoom))
    clips = [p for p, _ in plan if p]

    segments = []
    for i, text in enumerate(topic["slides"]):
        png, mp3, mp4 = OUT / f"s{i}.png", OUT / f"s{i}.mp3", OUT / f"s{i}.mp4"
        asyncio.run(tts(text, mp3))
        dur = duration(mp3) + 0.5
        enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-ar", "44100", "-ac", "2"]
        clip, zoom = plan[i]
        if clip:
            make_caption(text, png)
            zexpr = f"min(1.35,1+{zoom}*t)"
            fc = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                  "crop=1080:1920,setsar=1,fps=30,"
                  f"scale=w='trunc(1080*{zexpr}/2)*2':h='trunc(1920*{zexpr}/2)*2':eval=frame,"
                  "crop=1080:1920[bg];[bg][1:v]overlay=0:0[v]")
            cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip),
                   "-loop", "1", "-i", str(png), "-i", str(mp3),
                   "-filter_complex", fc,
                   "-map", "[v]", "-map", "2:a", "-af", "apad",
                   "-t", f"{dur:.2f}"] + enc + [str(mp4)]
        else:
            make_slide(text, png)
            cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(png), "-i", str(mp3),
                   "-af", "apad", "-t", f"{dur:.2f}", "-r", "30"] + enc + [str(mp4)]
        subprocess.run(cmd, check=True, capture_output=True)
        segments.append(mp4)

    lst = OUT / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in segments))
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "output.mp4"], check=True, capture_output=True)

    desc = topic["description"]
    if "Pexels" in SOURCES:
        desc += "\nلقطات الفيديو: Pexels - https://www.pexels.com"
    if "Pixabay" in SOURCES:
        desc += "\nلقطات الفيديو: Pixabay - https://pixabay.com"
    meta = {
        "title": f"{topic['title']} #Shorts",
        "description": desc,
        "tags": topic["tags"],
    }
    Path("meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    history.append(topic["key"])
    Path("history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Video length:", round(duration("output.mp4"), 1), "sec")


if __name__ == "__main__":
    main()
