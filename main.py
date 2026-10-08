import os
import re
import json
import random
import unicodedata
import asyncio
import subprocess
import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import edge_tts

# ==== CẤU HÌNH CHUNG ====
SHEET_ID = "112xs-BfIZykz-zsOUhPNnXT3cNcIVDlrVBYxelrtEdA"
DRIVE_OUTPUT_FOLDER_ID = "1OVLJi1TvnI1JL9Q7l8ApFfNH1Q8wWkTP"
NGUON_VIDEO_NEN_ROOT_ID = "1q8dWz0BvylzeN8hD5AyeX0_2Rs-Zmfrm"

VOICE = "vi-VN-HoaiMyNeural"
LOGO_PATH = "logo.png"
TEXT_LIEN_HE = "Thành Đạt Led - 0986474671 -  0867933396"
SO_VIDEO_NEN_MOI_LAN = (2, 3)
THOI_GIAN_HIEN_TOI_THIEU_GIAY = 1.8
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ==== MỖI TAB = 1 KIỂU VIDEO ====
# Cột cố định: A TieuDe, B NoiDung, C LoaiDen, E LinkDrive, F Status
PROFILES = {
    "lam_video_dai": dict(   # NGANG 16:9
        W=1280, H=720, thu_muc="Video Dai",
        logo_w=200, logo_y=20,
        contact_size=32, contact_y=50,
        sub_size=34, sub_margin_v=55, sub_margin_lr=80,
        so_tu_cum=11,
    ),
    "lam_video_ngan": dict(  # DỌC 9:16
        W=720, H=1280, thu_muc="Video Ngan",
        logo_w=140, logo_y=40,
        contact_size=24, contact_y=200,
        sub_size=42, sub_margin_v=260, sub_margin_lr=50,
        so_tu_cum=6,
    ),
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

sa_creds = SACredentials.from_service_account_file("service_account.json", scopes=SCOPES)
gc = gspread.authorize(sa_creds)
drive_read = build("drive", "v3", credentials=sa_creds)

oauth_creds = OAuthCredentials(
    token=None,
    refresh_token=os.environ["OAUTH_REFRESH_TOKEN"],
    client_id=os.environ["OAUTH_CLIENT_ID"],
    client_secret=os.environ["OAUTH_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/drive"],
)
drive_upload = build("drive", "v3", credentials=oauth_creds)

spreadsheet = gc.open_by_key(SHEET_ID)


# ================== TIỆN ÍCH ==================

def slugify_vi(text):
    text = text.lower().replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "video"


def get_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


def tim_folder(ten, parent_id, service):
    q = (f"name = '{ten.replace(chr(39), chr(92) + chr(39))}' "
         f"and mimeType = 'application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed = false")
    files = service.files().list(q=q, fields="files(id)").execute().get("files", [])
    return files[0]["id"] if files else None


def tim_hoac_tao_thu_muc(ten, parent_id, service):
    fid = tim_folder(ten, parent_id, service)
    if fid:
        return fid
    meta = {"name": ten, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return service.files().create(body=meta, fields="id").execute()["id"]


def lay_danh_sach_video(folder_id):
    return drive_read.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'video/' and trashed = false",
        fields="files(id, name)",
    ).execute().get("files", [])


def tai_video_ve(file_id, out_path):
    req = drive_read.files().get_media(fileId=file_id)
    with open(out_path, "wb") as f:
        dl = MediaIoBaseDownload(f, req)
        done = False
        while not done:
            _, done = dl.next_chunk()


# ================== TTS + PHỤ ĐỀ ==================

def giay_sang_ass_time(sec):
    h, m, s = int(sec // 3600), int((sec % 3600) // 60), int(sec % 60)
    return f"{h}:{m:02d}:{s:02d}.{int((sec - int(sec)) * 100):02d}"


async def tao_audio_va_phu_de(text, audio_path, ass_path, P):
    communicate = edge_tts.Communicate(text, VOICE, boundary="WordBoundary")
    words = []
    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                s = chunk["offset"] / 10_000_000
                words.append((s, s + chunk["duration"] / 10_000_000, chunk["text"]))

    n = P["so_tu_cum"]
    cums = []
    for idx in range(0, len(words), n):
        g = words[idx: idx + n]
        cums.append((g[0][0], g[-1][1], " ".join(w[2] for w in g)))

    for idx, (start, end, nd) in enumerate(cums):
        gioi_han = cums[idx + 1][0] if idx + 1 < len(cums) else end + THOI_GIAN_HIEN_TOI_THIEU_GIAY
        cums[idx] = (start, max(min(start + THOI_GIAN_HIEN_TOI_THIEU_GIAY, gioi_han), end), nd)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {P['W']}
PlayResY: {P['H']}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{P['sub_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,1,2,{P['sub_margin_lr']},{P['sub_margin_lr']},{P['sub_margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for s, e, nd in cums:
        nd = nd.replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{giay_sang_ass_time(s)},{giay_sang_ass_time(e)},Default,,0,0,0,,{nd}")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines))
    print(f"[DEBUG] {len(words)} từ, {len(cums)} cụm phụ đề")


# ================== VIDEO NỀN ==================

def chuan_bi_video_nen(folder_id, audio_duration, i, P):
    goc = lay_danh_sach_video(folder_id)
    if not goc:
        return None
    W, H = P["W"], P["H"]

    so_luong = min(random.randint(*SO_VIDEO_NEN_MOI_LAN), len(goc))
    chon = random.sample(goc, so_luong)
    random.shuffle(chon)

    paths, total = [], 0.0

    def tai(video):
        nonlocal total
        p = f"bgsrc_{i}_{len(paths)}.mp4"
        tai_video_ve(video["id"], p)
        paths.append(p)
        total += get_duration(p)

    for v in chon:
        tai(v)
    while total < audio_duration + 2 and len(paths) <= 40:
        tai(random.choice(goc))

    # scale + crop để lấp đầy khung (ngang hay dọc đều không bị méo/viền đen)
    parts = [
        f"[{j}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[v{j}]"
        for j in range(len(paths))
    ]
    parts.append("".join(f"[v{j}]" for j in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=0[bgv]")

    out = f"bgconcat_{i}.mp4"
    cmd = ["ffmpeg", "-y"]
    for p in paths:
        cmd += ["-i", p]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[bgv]", "-an", out]
    subprocess.run(cmd, check=True)

    for p in paths:
        os.remove(p)
    return out


# ================== GHÉP CUỐI ==================

def ghep_video(audio_path, background_video, ass_path, out_path, P):
    ass_abs = os.path.abspath(ass_path)
    if not os.path.exists(ass_abs):
        raise FileNotFoundError(f"Không tìm thấy file phụ đề: {ass_abs}")
    ass_esc = ass_abs.replace(":", r"\:")

    fc = (
        f"[0:v]scale={P['W']}:{P['H']}[bg];"
        f"[1:v]scale={P['logo_w']}:-1[logo];"
        f"[bg][logo]overlay=W-w-20:{P['logo_y']}[bg2];"
        f"[bg2]drawtext=fontfile={FONT_BOLD}:text='{TEXT_LIEN_HE}':"
        f"fontsize={P['contact_size']}:fontcolor=#FF8A00:"
        f"borderw=2:bordercolor=black@0.8:box=1:boxcolor=black@0.4:boxborderw=14:"
        f"x=(w-text_w)/2:y={P['contact_y']}[bg3];"
        f"[bg3]subtitles=filename='{ass_esc}':fontsdir=/usr/share/fonts/truetype/dejavu[outv]"
    )
    subprocess.run([
        "ffmpeg", "-y",
        "-i", background_video, "-i", LOGO_PATH, "-i", audio_path,
        "-filter_complex", fc,
        "-map", "[outv]", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", out_path,
    ], check=True)


def upload_to_drive(file_path, file_name, parent_id):
    media = MediaFileUpload(file_path, resumable=True)
    f = drive_upload.files().create(
        body={"name": file_name, "parents": [parent_id]}, media_body=media, fields="id"
    ).execute()
    drive_upload.permissions().create(
        fileId=f["id"], body={"type": "anyone", "role": "reader"}
    ).execute()
    return f"https://drive.google.com/file/d/{f['id']}/view"


# ================== MAIN ==================

def xu_ly_tab(ten_tab, P):
    try:
        ws = spreadsheet.worksheet(ten_tab)
    except gspread.WorksheetNotFound:
        print(f"Không có tab {ten_tab}, bỏ qua.")
        return

    rows = ws.get_all_values()
    thu_muc_kieu = tim_hoac_tao_thu_muc(P["thu_muc"], DRIVE_OUTPUT_FOLDER_ID, drive_upload)

    for i, row in enumerate(rows[1:], start=2):
        row = row + [""] * (6 - len(row))
        tieu_de, text, loai_den = row[0].strip(), row[1].strip(), row[2].strip()
        status = row[5].strip()

        if status.lower() == "done" or not (tieu_de and text and loai_den):
            continue
        if text == "Đang tạo nội dung...":
            continue

        folder_id = tim_folder(loai_den, NGUON_VIDEO_NEN_ROOT_ID, drive_read)
        if not folder_id:
            print(f"[{ten_tab}] Dòng {i}: không có thư mục nền '{loai_den}', bỏ qua.")
            continue

        audio_path, ass_path, out_path = f"audio_{i}.mp3", f"caption_{i}.ass", f"output_{i}.mp4"
        bg = None
        try:
            asyncio.run(tao_audio_va_phu_de(text, audio_path, ass_path, P))
            bg = chuan_bi_video_nen(folder_id, get_duration(audio_path), i, P)
            if not bg:
                print(f"[{ten_tab}] Dòng {i}: thư mục '{loai_den}' không có video, bỏ qua.")
                continue

            ghep_video(audio_path, bg, ass_path, out_path, P)

            sub_id = tim_hoac_tao_thu_muc(loai_den, thu_muc_kieu, drive_upload)
            link = upload_to_drive(out_path, slugify_vi(tieu_de) + ".mp4", sub_id)

            ws.update_cell(i, 5, link)
            ws.update_cell(i, 6, "Done")
            print(f"[{ten_tab}] Dòng {i}: xong -> {link}")
        except Exception as e:
            print(f"[{ten_tab}] Dòng {i}: LỖI {e}")
            ws.update_cell(i, 9, f"❌ {str(e)[:150]}")
        finally:
            for p in (audio_path, ass_path, bg, out_path):
                if p and os.path.exists(p):
                    os.remove(p)


def main():
    for ten_tab, P in PROFILES.items():
        xu_ly_tab(ten_tab, P)


if __name__ == "__main__":
    main()
