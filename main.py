import os
import re
import sys
import json
import time
import random
import unicodedata
import asyncio
import subprocess
import urllib.request
import urllib.error
from xml.sax.saxutils import escape as xml_escape
from itertools import zip_longest

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

# Giọng đọc (đều là edge-tts nên phụ đề vẫn khớp từng từ). Mỗi loại video có giọng riêng,
# giọng còn lại là dự phòng. Muốn đổi giọng: sửa mục "voices" của từng tab bên dưới.
GIONG_HOAI_MY = "vi-VN-HoaiMyNeural"   # nữ
GIONG_NAM_MINH = "vi-VN-NamMinhNeural"  # nam
SO_LAN_THU_TTS = 3
# Dự phòng chính thức (tuỳ chọn): Azure Speech - cùng giọng HoaiMy/NamMinh. Chỉ dùng khi có Secret
# AZURE_SPEECH_KEY và AZURE_SPEECH_REGION (ví dụ southeastasia). Không có thì bỏ qua.
AZURE_KEY = os.environ.get("AZURE_SPEECH_KEY", "").strip()
AZURE_REGION = os.environ.get("AZURE_SPEECH_REGION", "").strip()
GIAN_CACH_TTS_GIAY = 2  # nghỉ ngắn trước mỗi lần gọi TTS cho đỡ bị Microsoft giới hạn tần suất
LOGO_PATH = "logo.png"
TEXT_LIEN_HE = "Thành Đạt Led - 0986474671 -  0867933396"
SO_VIDEO_NEN_MOI_LAN = (2, 3)
THOI_GIAN_HIEN_TOI_THIEU_GIAY = 1.8
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Sau chừng này phút thì không bắt đầu video mới nữa (lần chạy sau làm tiếp). Workflow timeout = 350.
GIOI_HAN_PHUT = 300
# 0 = không giới hạn; đặt biến môi trường MAX_VIDEO=6 nếu muốn mỗi lần chạy tối đa 6 video
MAX_VIDEO = int(os.environ.get("MAX_VIDEO", "0") or 0)

DONE = ("done", "hoàn thành", "hoan thanh")

# ==== MỖI TAB = 1 KIỂU VIDEO (thứ tự ở đây = thứ tự xen kẽ: dài, ngắn, dài, ngắn...) ====
# Cột cố định: A TieuDe, B NoiDung, C LoaiDen, E LinkDrive, F Status, I Log
PROFILES = {
    "lam_video_dai": dict(   # NGANG 16:9
        W=1280, H=720, thu_muc="Video Dai",
        voices=[GIONG_HOAI_MY, GIONG_NAM_MINH],
        logo_w=200, logo_y=20,
        contact_size=32, contact_y=50,
        sub_size=34, sub_margin_v=55, sub_margin_lr=80,
        so_tu_cum=11,
    ),
    "lam_video_ngan": dict(  # DỌC 9:16
        W=720, H=1280, thu_muc="Video Ngan",
        voices=[GIONG_NAM_MINH, GIONG_HOAI_MY],
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

gc = drive_read = drive_upload = spreadsheet = None


def khoi_tao():
    global gc, drive_read, drive_upload, spreadsheet
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

class TTSBlocked(Exception):
    """edge-tts (cả 2 giọng) đều không tạo được giọng đọc."""


def retry(fn, *args, tries=5, **kwargs):
    """Gọi Google API, lỗi tạm thời (429/500/503/mạng) thì đợi rồi thử lại."""
    for lan in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if lan == tries - 1:
                raise
            cho = 15 * (lan + 1)
            print(f"[RETRY {lan + 1}/{tries - 1}] {type(e).__name__}: {str(e)[:100]} - đợi {cho}s", flush=True)
            time.sleep(cho)


def ghi_log(ws, i, msg):
    try:
        retry(ws.update_cell, i, 9, msg)
    except Exception as e:
        print(f"Không ghi được log vào Sheet: {e}", flush=True)


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


# ================== GIỌNG ĐỌC (edge-tts, 2 giọng luân phiên) + PHỤ ĐỀ ==================

def giay_sang_ass_time(sec):
    h, m, s = int(sec // 3600), int((sec % 3600) // 60), int(sec % 60)
    return f"{h}:{m:02d}:{s:02d}.{int((sec - int(sec)) * 100):02d}"


def uoc_luong_tu(text, audio_path):
    """Không có mốc thời gian từng từ -> chia đều theo độ dài chữ trên tổng thời lượng audio."""
    dur = get_duration(audio_path)
    tu = text.split()
    if not tu:
        return []
    trong = [len(t) + 1 for t in tu]
    tong, t0, words = sum(trong), 0.0, []
    for t, w in zip(tu, trong):
        d = dur * w / tong
        words.append((t0, t0 + d, t))
        t0 += d
    return words


async def _edge_tts(text, audio_path, voice):
    communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    words = []
    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                s = chunk["offset"] / 10_000_000
                words.append((s, s + chunk["duration"] / 10_000_000, chunk["text"]))
    return words


def tao_giong_azure(text, audio_path, voice):
    """Azure Speech (REST). Không có mốc thời gian từng từ -> phụ đề ước lượng theo độ dài chữ."""
    ssml = ("<speak version='1.0' xml:lang='vi-VN'><voice name='%s'>%s</voice></speak>"
            % (voice, xml_escape(text)))
    req = urllib.request.Request(
        f"https://{AZURE_REGION}.tts.speech.microsoft.com/cognitiveservices/v1",
        data=ssml.encode("utf-8"),
        headers={
            "Ocp-Apim-Subscription-Key": AZURE_KEY,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
            "User-Agent": "video-automation",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(audio_path, "wb") as f:
            f.write(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Azure HTTP {e.code}: {e.read()[:150]!r}")
    if os.path.getsize(audio_path) < 1000:
        raise RuntimeError("Azure trả audio rỗng")
    return uoc_luong_tu(text, audio_path)


def tao_giong_doc(text, audio_path, voices):
    """Trả về danh sách (bắt đầu, kết thúc, từ).
    1) edge-tts luân phiên 2 giọng, 2) nếu hỏng và có key thì dùng Azure Speech, 3) không được thì TTSBlocked."""
    loi = None
    for lan in range(SO_LAN_THU_TTS):
        voice = voices[lan % len(voices)]
        try:
            time.sleep(GIAN_CACH_TTS_GIAY)
            words = asyncio.run(_edge_tts(text, audio_path, voice))
            if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
                raise RuntimeError("audio rỗng")
            if not words:
                print("[TTS] không có mốc từ, ước lượng thời gian phụ đề.", flush=True)
                words = uoc_luong_tu(text, audio_path)
            return words
        except Exception as e:
            loi = e
            cho = min(10 * (lan + 1), 30)
            print(f"[TTS {lan + 1}/{SO_LAN_THU_TTS}] edge {voice}: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if lan < SO_LAN_THU_TTS - 1:
                time.sleep(cho)

    if AZURE_KEY and AZURE_REGION:
        for voice in voices:
            try:
                words = tao_giong_azure(text, audio_path, voice)
                print(f"[TTS] dùng Azure Speech, giọng {voice}", flush=True)
                return words
            except Exception as e:
                loi = e
                print(f"[TTS Azure] {voice}: {str(e)[:150]}", flush=True)
    raise TTSBlocked(f"edge-tts lỗi, Azure {'lỗi' if AZURE_KEY else 'chưa cấu hình'}: {str(loi)[:120]}")


def viet_phu_de(words, ass_path, P):
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
    print(f"[DEBUG] {len(words)} từ, {len(cums)} cụm phụ đề", flush=True)


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

    try:
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
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for p in paths:
            cmd += ["-i", p]
        cmd += ["-filter_complex", ";".join(parts), "-map", "[bgv]", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", out]
        subprocess.run(cmd, check=True)
        return out
    finally:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)


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
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", background_video, "-i", LOGO_PATH, "-i", audio_path,
        "-filter_complex", fc,
        "-map", "[outv]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac",
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


# ================== HÀNG ĐỢI XEN KẼ ==================

def lay_viec_cho():
    """Đọc cả 2 tab, trả về danh sách việc XEN KẼ: dài, ngắn, dài, ngắn... (tab nào hết thì làm tiếp tab còn lại)."""
    theo_tab = []
    for ten_tab in PROFILES:
        try:
            ws = retry(spreadsheet.worksheet, ten_tab)
        except gspread.WorksheetNotFound:
            print(f"Không có tab {ten_tab}, bỏ qua.", flush=True)
            continue
        rows = retry(ws.get_all_values)
        ds = []
        for i, row in enumerate(rows[1:], start=2):
            row = row + [""] * (6 - len(row))
            tieu_de, text, loai_den = row[0].strip(), row[1].strip(), row[2].strip()
            if row[5].strip().lower() in DONE or not (tieu_de and text and loai_den):
                continue
            if text == "Đang tạo nội dung...":
                continue
            ds.append(dict(tab=ten_tab, ws=ws, i=i, tieu_de=tieu_de, text=text, loai_den=loai_den))
        print(f"[{ten_tab}] {len(ds)} video cần làm.", flush=True)
        theo_tab.append(ds)

    xen_ke = []
    for nhom in zip_longest(*theo_tab):
        xen_ke.extend(v for v in nhom if v is not None)
    return xen_ke


# ================== LÀM 1 VIDEO ==================

cache_nen, cache_thu_muc_out = {}, {}


def lam_1_video(job):
    ten_tab, ws, i = job["tab"], job["ws"], job["i"]
    tieu_de, text, loai_den = job["tieu_de"], job["text"], job["loai_den"]
    P = PROFILES[ten_tab]

    if loai_den not in cache_nen:
        cache_nen[loai_den] = retry(tim_folder, loai_den, NGUON_VIDEO_NEN_ROOT_ID, drive_read)
    folder_id = cache_nen[loai_den]
    if not folder_id:
        cache_nen.pop(loai_den, None)
        print(f"[{ten_tab}] Dòng {i}: không có thư mục nền '{loai_den}', bỏ qua.", flush=True)
        ghi_log(ws, i, f"❌ Không có thư mục nền '{loai_den}'")
        return

    audio_path, ass_path, out_path = f"audio_{ten_tab}_{i}.mp3", f"caption_{ten_tab}_{i}.ass", f"output_{ten_tab}_{i}.mp4"
    bg = None
    try:
        words = tao_giong_doc(text, audio_path, P["voices"])
        viet_phu_de(words, ass_path, P)
        bg = chuan_bi_video_nen(folder_id, get_duration(audio_path), f"{ten_tab}_{i}", P)
        if not bg:
            print(f"[{ten_tab}] Dòng {i}: thư mục '{loai_den}' không có video, bỏ qua.", flush=True)
            ghi_log(ws, i, f"❌ Thư mục '{loai_den}' không có video")
            return

        ghep_video(audio_path, bg, ass_path, out_path, P)

        if ten_tab not in cache_thu_muc_out:
            cache_thu_muc_out[ten_tab] = retry(tim_hoac_tao_thu_muc, P["thu_muc"], DRIVE_OUTPUT_FOLDER_ID, drive_upload)
        khoa = (ten_tab, loai_den)
        if khoa not in cache_thu_muc_out:
            cache_thu_muc_out[khoa] = retry(tim_hoac_tao_thu_muc, loai_den, cache_thu_muc_out[ten_tab], drive_upload)
        link = retry(upload_to_drive, out_path, slugify_vi(tieu_de) + ".mp4", cache_thu_muc_out[khoa])

        # 1 lệnh ghi duy nhất (link + Done + xoá log) để không vượt hạn mức 60 lệnh ghi/phút
        retry(ws.batch_update, [
            {"range": f"E{i}:F{i}", "values": [[link, "Done"]]},
            {"range": f"I{i}", "values": [[""]]},
        ], value_input_option="USER_ENTERED")
        print(f"[{ten_tab}] Dòng {i}: xong -> {link}", flush=True)
    except TTSBlocked:
        raise
    except Exception as e:
        print(f"[{ten_tab}] Dòng {i}: LỖI {type(e).__name__}: {e}", flush=True)
        ghi_log(ws, i, f"❌ {type(e).__name__}: {str(e)[:140]}")
    finally:
        for p in (audio_path, ass_path, bg, out_path):
            if p and os.path.exists(p):
                os.remove(p)


# ================== MAIN ==================

def main():
    bat_dau = time.time()
    khoi_tao()
    viec = lay_viec_cho()
    print(f"Tổng cộng {len(viec)} video, làm xen kẽ giữa các tab.", flush=True)

    da_lam, bi_chan_tts = 0, False
    for job in viec:
        if (time.time() - bat_dau) / 60 > GIOI_HAN_PHUT:
            print("Hết thời gian của lần chạy này, phần còn lại để lần sau.", flush=True)
            break
        if MAX_VIDEO and da_lam >= MAX_VIDEO:
            print(f"Đã đủ {MAX_VIDEO} video, dừng.", flush=True)
            break
        try:
            lam_1_video(job)
        except TTSBlocked as e:
            print(f"Không có giọng đọc nào dùng được, dừng sớm: {e}", flush=True)
            bi_chan_tts = True
            break
        except Exception as e:  # lỗi bất ngờ ở 1 video không được làm sập cả lần chạy
            print(f"[{job['tab']}] Dòng {job['i']}: LỖI NGOÀI DỰ KIẾN {type(e).__name__}: {e}", flush=True)
        da_lam += 1

    print(f"Đã xử lý {da_lam} video.", flush=True)
    if bi_chan_tts:
        sys.exit(1)   # để GitHub báo ĐỎ, không báo xanh khi chưa dựng được video nào


if __name__ == "__main__":
    main()
