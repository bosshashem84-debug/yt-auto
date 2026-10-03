import json, os, sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

video = sys.argv[1] if len(sys.argv) > 1 else "output.mp4"
meta = json.load(open("meta.json", encoding="utf-8"))

creds = Credentials(
    None,
    refresh_token=os.environ["YT_REFRESH_TOKEN"],
    client_id=os.environ["YT_CLIENT_ID"],
    client_secret=os.environ["YT_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/youtube.upload"]
)

youtube = build("youtube", "v3", credentials=creds)

body = {
    'snippet': {
        'title': meta.get('title', 'Car Short #shorts'),
        'description': meta.get('description', ''),
        'tags': meta.get('tags', []),
        'categoryId': '2'  # فئة السيارات والمركبات (Autos & Vehicles)
    },
    'status': {
        'privacyStatus': 'public',         # نشر علني مباشر وفوري
        'selfDeclaredMadeForKids': False   # غير مخصص للأطفال
    }
}

media = MediaFileUpload(video, mimetype="video/mp4", resumable=True)
request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

response = None
while response is None:
    status, response = request.next_chunk()
    if status:
        print(f"Uploaded {int(status.progress() * 100)}%")

print(f"Done: https://youtube.com/watch?v={response['id']}")
