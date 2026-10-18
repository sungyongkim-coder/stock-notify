"""회의 녹음 -> 전사 -> 요약 -> 바탕화면 저장.

사용법: python meeting.py            (Enter 치면 녹음 종료 후 자동 처리)
        python meeting.py a.wav b.wav (이미 있는 녹음 파일로 회의록만 뽑기)

녹음하는 동안 CHUNK초씩 미리 전사해 두므로, 회의가 끝나면 마지막 몇십 초만 전사하면 된다.
전사는 세그먼트마다 .txt.part에 적어두므로 중간에 끊겨도 다시 돌리면 이어서 한다.
"""

import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import warnings
import wave
import winreg

import numpy as np
import soundcard as sc

warnings.filterwarnings("ignore", message="data discontinuity in recording")

SR = 48000
BLOCK = SR // 10
# ponytail: 정확도/속도 조절 손잡이. 비우면 GPU는 large-v3-turbo, CPU는 small.
#           base는 한국어가 거의 알아볼 수 없는 수준이라 기본값에서 뺐다.
#           녹음 중에 미리 전사하므로 CPU가 괜찮으면 medium도 회의 길이 안에 따라잡는다.
#           바꾼 뒤 다시 뽑으려면 기존 .txt 전사본을 지워야 캐시를 안 쓴다.
#           base/small/medium은 로컬 캐시에 있다. 그 외 모델은 첫 실행에 인터넷이 필요하다.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "")
# WHISPER_DEVICE=cpu 로 GPU 자동 사용을 끌 수 있다.
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
CHUNK = 30          # 녹음 중 이만큼(초) 쌓일 때마다 미리 전사

# 전사 엔진. local(기본, 무료·오프라인) / rtzr(리턴제로) / clova(네이버 CLOVA Speech).
# API는 1시간 녹음을 몇 분 안에, 화자 구분까지 해서 돌려준다. 실패하면 local로 넘어간다.
#   rtzr : RTZR_CLIENT_ID, RTZR_CLIENT_SECRET  (developers.rtzr.ai 콘솔에서 발급)
#   clova: CLOVA_INVOKE_URL, CLOVA_SECRET      (네이버 클라우드 CLOVA Speech 도메인의 Invoke URL / Secret Key)
STT = os.getenv("STT", "local").lower()
# 고유명사 인식률을 올릴 단어들, 쉼표로 구분. 예) set STT_KEYWORDS=H마트,한인마트,미국법인
STT_KEYWORDS = [k.strip() for k in os.getenv("STT_KEYWORDS", "").split(",") if k.strip()]

# 프롬프트는 stdin으로 넘기므로 여러 줄이어도 된다(명령줄 인자는 cmd.exe가 망가뜨릴 수 있다).
PROMPT = (
    "입력된 업무 회의 녹취록을 한국어 회의록으로 정리하세요. "
    "반드시 이 5개 제목을 순서대로 그대로 사용하세요: "
    "'## 한 줄 요약', '## 핵심 논의', '## 결정사항', '## 액션 아이템', '## 후속 확인 필요'. "
    "액션 아이템은 '담당자 / 할 일 / 기한' 형태로 쓰고 언급이 없으면 '미지정'이라고 적으세요. "
    "녹취에 없는 내용은 지어내지 말고, 알아듣기 어려운 부분은 '(불명확)'으로 표시하세요. "
    "줄이 '화자N:'으로 시작하면 자동 화자 구분 결과이니 누가 한 말인지 구분하는 데 참고하세요. "
    "서두나 맺음말 없이 회의록 본문만 출력하세요."
)


def desktop():
    """OneDrive 리다이렉트를 반영한 실제 바탕화면 경로."""
    key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        return winreg.QueryValueEx(k, "Desktop")[0]


def mix(*tracks):
    """길이가 다른 모노 트랙들을 합쳐 클리핑 없이 정규화."""
    tracks = [t for t in tracks if len(t)]
    if not tracks:
        return np.zeros(0, "float32")
    n = max(len(t) for t in tracks)
    out = sum(np.pad(t, (0, n - len(t))) for t in tracks)
    peak = float(np.abs(out).max())
    return out / peak * 0.95 if peak > 1.0 else out


class Track(threading.Thread):
    """한 입력 장치를 Enter 칠 때까지 계속 읽어들이는 스레드."""

    def __init__(self, mic, label):
        super().__init__(daemon=True)
        self.mic, self.label = mic, label
        self.frames = []
        self.error = None
        self.stopping = threading.Event()

    def run(self):
        try:
            with self.mic.recorder(samplerate=SR, blocksize=BLOCK) as rec:
                while not self.stopping.is_set():
                    self.frames.append(rec.record(numframes=BLOCK))
        except Exception as e:
            self.error = e

    def mono(self):
        if not self.frames:
            return np.zeros(0, "float32")
        return to_mono(np.concatenate(self.frames))

    def length(self):
        return sum(len(f) for f in list(self.frames))

    def segment(self, start, end):
        """[start, end) 샘플 구간의 모노 신호. 녹음 중에도 부를 수 있다."""
        out, pos = [], 0
        for f in list(self.frames):
            if pos >= end:
                break
            if pos + len(f) > start:
                out.append(f[max(0, start - pos):end - pos])
            pos += len(f)
        return to_mono(np.concatenate(out)) if out else np.zeros(0, "float32")


def to_mono(a):
    return (a.mean(axis=1) if a.ndim > 1 else a).astype("float32")


def open_tracks():
    """마이크(내 목소리) + 스피커 루프백(화상회의 상대방 목소리)."""
    tracks = []
    try:
        tracks.append(Track(sc.default_microphone(), "마이크"))
    except Exception as e:
        print(f"  마이크 없음: {e}")
    try:
        spk = sc.default_speaker().name
        tracks.append(Track(sc.get_microphone(spk, include_loopback=True), "시스템 소리"))
    except Exception as e:
        print(f"  시스템 소리 캡처 불가: {e}")
    return tracks


def write_wav(path, mono, sr=SR):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes())


def resume_from(part_path):
    """중단된 전사의 (이미 받아쓴 줄들, 다시 시작할 초) 를 돌려준다."""
    lines, offset = [], 0.0
    if os.path.exists(part_path):
        with open(part_path, encoding="utf-8") as f:
            for line in f:
                end, _, text = line.rstrip("\n").partition("\t")
                if text:
                    lines.append(text)
                    offset = float(end)
    return lines, offset


_model = None


def load_model():
    """Whisper 모델을 한 번만 올린다. CUDA GPU가 쓸 수 있으면 GPU, 아니면 CPU."""
    global _model
    if _model is not None:
        return _model
    from faster_whisper import WhisperModel

    if WHISPER_DEVICE != "cpu":
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                m = WhisperModel(WHISPER_MODEL or "large-v3-turbo", device="cuda",
                                 compute_type="float16")
                list(m.transcribe(np.zeros(16000, "float32"))[0])  # cuDNN 등 누락을 여기서 확인
                print(f"  전사: GPU, {WHISPER_MODEL or 'large-v3-turbo'}")
                _model = m
                return _model
        except Exception as e:
            print(f"  GPU 사용 불가, CPU로 전사합니다: {e}")

    # 논리코어 전부보다 물리코어 수(대략 절반)가 더 빠르고, 녹음 중 화상회의도 덜 버벅인다.
    _model = WhisperModel(WHISPER_MODEL or "small", device="cpu", compute_type="int8",
                          cpu_threads=max(1, (os.cpu_count() or 4) // 2))
    print(f"  전사: CPU, {WHISPER_MODEL or 'small'}")
    return _model


# beam_size=1(그리디)이 빔서치보다 몇 배 빠르다. 정확도는 조금 손해.
OPTS = dict(language="ko", vad_filter=True, beam_size=1, condition_on_previous_text=False)


def quiet_cut(x, search=5.0, win=0.1):
    """x 끝쪽 search초 안에서 가장 조용한 지점. 말 중간에서 자르지 않으려고."""
    w = int(SR * win)
    tail = x[-int(SR * search):]
    n = len(tail) // w
    if n == 0:
        return len(x)
    energy = (tail[:n * w].reshape(n, w) ** 2).mean(axis=1)
    return len(x) - len(tail) + int(energy.argmin()) * w + w // 2


def next_start(start, cut_end, last_end):
    """다음 덩어리 시작(초). 마지막 문장 끝부터 다시 보되, 되돌아가는 건 최대 3초."""
    return max(last_end or start, cut_end - 3.0)


class LiveTranscriber(threading.Thread):
    """녹음하는 동안 CHUNK초씩 미리 전사해 .txt.part에 적는다.
    녹음이 끝나면 transcribe()가 .part의 마지막 지점부터 남은 부분만 이어서 한다."""

    def __init__(self, tracks, wav_path):
        super().__init__(daemon=True)
        self.tracks = tracks
        self.part = os.path.splitext(wav_path)[0] + ".txt.part"
        self.scratch = os.path.splitext(wav_path)[0] + ".live.wav"
        self.done = 0.0
        self.stopping = threading.Event()

    def available(self):
        lens = [t.length() for t in self.tracks]
        # 한 장치가 멈춰 있으면(예: 아무 소리도 안 나는 스피커 루프백) 기다리지 않는다.
        return max(lens) if max(lens) - min(lens) > 10 * SR else min(lens)

    def run(self):
        from faster_whisper.audio import decode_audio
        try:
            model = load_model()
            while not self.stopping.is_set():
                start = int(self.done * SR)
                if self.available() - start < CHUNK * SR:
                    self.stopping.wait(1)
                    continue
                x = mix(*(t.segment(start, start + CHUNK * SR) for t in self.tracks))
                x = x[:quiet_cut(x)]
                write_wav(self.scratch, x)
                segments, _ = model.transcribe(decode_audio(self.scratch), **OPTS)
                last_end = None
                with open(self.part, "a", encoding="utf-8") as f:
                    for s in segments:
                        text = s.text.strip()
                        if text:
                            last_end = self.done + s.end
                            f.write(f"{last_end:.2f}\t{text}\n")
                            f.flush()
                self.done = next_start(self.done, self.done + len(x) / SR, last_end)
                lag = self.available() / SR - self.done
                print(f"\r  (미리 전사 {self.done / 60:.0f}분째, 밀린 분량 {lag:.0f}초)   ",
                      end="", flush=True)
        except Exception as e:
            print(f"\n  녹음 중 전사 중단(녹음은 계속됩니다): {e}")
        finally:
            if os.path.exists(self.scratch):
                os.remove(self.scratch)


def by_speaker(utterances):
    """[(화자, 말)] -> '화자N: ...' 줄들. 같은 사람이 이어서 한 말은 한 줄로 합친다."""
    lines, prev = [], None
    for spk, text in utterances:
        text = text.strip()
        if not text:
            continue
        if spk == prev:
            lines[-1] += " " + text
        else:
            lines.append(f"화자{spk}: {text}")
            prev = spk
    return "\n".join(lines)


def upload_copy(wav_path):
    """업로드용 16kHz 모노 wav(48kHz 원본의 1/3 크기). 반환값은 임시 파일 경로."""
    from faster_whisper.audio import decode_audio
    path = os.path.splitext(wav_path)[0] + ".16k.wav"
    write_wav(path, decode_audio(wav_path), sr=16000)
    return path


def transcribe_rtzr(path):
    import requests
    api = "https://openapi.vito.ai/v1"
    r = requests.post(f"{api}/authenticate", timeout=30, data={
        "client_id": os.environ["RTZR_CLIENT_ID"],
        "client_secret": os.environ["RTZR_CLIENT_SECRET"]})
    r.raise_for_status()
    auth = {"Authorization": "bearer " + r.json()["access_token"]}
    config = {"use_diarization": True, "diarization": {"spk_count": 0},   # 0 = 화자 수 자동
              "use_itn": True, "use_disfluency_filter": True}
    if STT_KEYWORDS:
        config["keywords"] = STT_KEYWORDS
    with open(path, "rb") as f:
        r = requests.post(f"{api}/transcribe", headers=auth, timeout=1800,
                          files={"file": f}, data={"config": json.dumps(config)})
    r.raise_for_status()
    job = r.json()["id"]
    deadline = time.time() + 3600
    while time.time() < deadline:
        time.sleep(5)
        r = requests.get(f"{api}/transcribe/{job}", headers=auth, timeout=30)
        if r.status_code == 429:            # 조회가 너무 잦음. 잠시 뒤 다시
            continue
        r.raise_for_status()
        res = r.json()
        if res["status"] == "completed":
            return by_speaker((u["spk"] + 1, u["msg"]) for u in res["results"]["utterances"])
        if res["status"] == "failed":
            raise RuntimeError(f"RTZR 전사 실패: {res.get('error')}")
    raise RuntimeError("RTZR 전사가 1시간 안에 끝나지 않았습니다")


def transcribe_clova(path):
    import requests
    params = {"language": "ko-KR", "completion": "sync", "fullText": True,
              "diarization": {"enable": True}}
    if STT_KEYWORDS:
        params["boostings"] = [{"words": ",".join(STT_KEYWORDS)}]
    with open(path, "rb") as f:
        r = requests.post(
            os.environ["CLOVA_INVOKE_URL"].rstrip("/") + "/recognizer/upload", timeout=3600,
            headers={"Accept": "application/json;UTF-8",
                     "X-CLOVASPEECH-API-KEY": os.environ["CLOVA_SECRET"]},
            files={"media": f, "params": (None, json.dumps(params, ensure_ascii=False).encode(),
                                          "application/json")})
    r.raise_for_status()
    res = r.json()
    if res.get("result") != "COMPLETED":
        raise RuntimeError(f"CLOVA 전사 실패: {res.get('message')}")
    return by_speaker((s.get("speaker", {}).get("label", "?"), s["text"]) for s in res["segments"])


def transcribe(wav_path):
    """전사. 완성본은 .txt로 캐시하고, 진행 중에는 .txt.part에 한 줄씩 적는다."""
    from faster_whisper import BatchedInferencePipeline
    from faster_whisper.audio import decode_audio

    txt_path = os.path.splitext(wav_path)[0] + ".txt"
    if os.path.exists(txt_path):
        print("  기존 전사본 재사용")
        return open(txt_path, encoding="utf-8").read().strip()

    api = {"rtzr": transcribe_rtzr, "clova": transcribe_clova}.get(STT)
    if api:
        print(f"  {STT} API로 전사 (업로드 후 보통 몇 분)")
        upload = None
        try:
            upload = upload_copy(wav_path)
            out = api(upload)
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(out + "\n")
            return out
        except Exception as e:
            print(f"  {STT} API 실패, 로컬 Whisper로 전사합니다: {e}")
        finally:
            if upload and os.path.exists(upload):
                os.remove(upload)

    part_path = txt_path + ".part"
    lines, offset = resume_from(part_path)
    if offset:
        print(f"  이어서 전사: {offset / 60:.0f}분 지점부터")

    audio = decode_audio(wav_path)[int(offset * 16000):]   # decode_audio는 16kHz 모노
    # 말소리 구간들을 묶어서 한꺼번에 돌린다. GPU에서 특히 몇 배 빠르다.
    segments, _ = BatchedInferencePipeline(model=load_model()).transcribe(
        audio, batch_size=8, **OPTS)

    with open(part_path, "a", encoding="utf-8") as f:
        for s in segments:
            text = s.text.strip()
            if not text:
                continue
            lines.append(text)
            f.write(f"{offset + s.end:.2f}\t{text}\n")
            f.flush()
            print(f"\r  전사 {(offset + s.end) / 60:.0f}분째", end="", flush=True)
    print()

    out = "\n".join(lines)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(out + "\n")
    os.remove(part_path)
    return out


def summarize(transcript):
    """Claude Code CLI로 요약. 기존 Pro 구독을 쓰므로 API 키가 필요 없다."""
    import subprocess

    exe = shutil.which("claude.cmd") or shutil.which("claude")
    if not exe:
        return "(claude CLI를 찾을 수 없어 요약을 건너뜁니다. 아래 전문을 참고하세요.)"
    # 요약에는 도구가 필요 없으므로 권한 우회(bypassPermissions)는 쓰지 않는다.
    try:
        r = subprocess.run(
            [exe, "-p"], input=f"{PROMPT}\n\n<녹취록>\n{transcript}\n</녹취록>\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        )
    except subprocess.TimeoutExpired:
        return "(요약 실패: 15분 안에 응답이 없었습니다. 아래 전문을 참고하세요.)"
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        # CLI는 사용량 초과 같은 오류를 stdout에 찍기도 하므로 둘 다 본다.
        why = (r.stderr.strip() or out)[:500] or f"빈 응답, 종료 코드 {r.returncode}"
        return f"(요약 실패: {why}. 아래 전문을 참고하세요.)"
    return out


def to_docx(md_path):
    """우리가 쓴 마크다운을 워드 문서로 변환. 반환값은 .docx 경로."""
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "맑은 고딕"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "맑은 고딕")

    for raw in open(md_path, encoding="utf-8").read().splitlines():
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", raw).rstrip()
        if not line or set(line) <= set("-|= "):   # 빈 줄, 구분선, 표 구분행
            continue
        if line.startswith("### "):
            doc.add_heading(line[4:], 2)
        elif line.startswith("## "):
            doc.add_heading(line[3:], 1)
        elif line.startswith("# "):
            doc.add_heading(line[2:], 0)
        elif line.startswith(("- ", "* ")):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif line.startswith("|"):
            doc.add_paragraph("   ".join(c.strip() for c in line.strip("|").split("|")))
        else:
            doc.add_paragraph(line)

    out = os.path.splitext(md_path)[0] + ".docx"
    doc.save(out)
    return out


def report(wav_path):
    """녹음 파일 하나 -> 전사 -> 요약 -> .md/.docx. 이미 있는 wav에도 그대로 쓴다."""
    wav_path = os.path.abspath(wav_path)
    base = os.path.splitext(os.path.basename(wav_path))[0]
    with wave.open(wav_path, "rb") as w:
        secs = w.getnframes() / w.getframerate()
    dur = f"{secs:.0f}초" if secs < 60 else f"{secs / 60:.0f}분"
    try:
        stamp = datetime.datetime.strptime(base, "회의록_%Y-%m-%d_%H%M")
    except ValueError:                  # 이름이 다르면 파일 수정시각을 회의 시각으로
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(wav_path))

    print(f"\n[{base}] {dur}. 전사 중…")
    transcript = transcribe(wav_path)
    if not transcript:
        sys.exit(f"전사 결과가 비었습니다. 녹음 파일: {wav_path}")

    print("요약 중…")
    summary = summarize(transcript)

    md_path = os.path.splitext(wav_path)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 회의록 {stamp:%Y-%m-%d %H:%M} ({dur})\n\n")
        f.write(summary)
        f.write("\n\n---\n\n## 전체 녹취록\n\n" + transcript + "\n")

    try:
        out = to_docx(md_path)
    except Exception as e:              # 워드 변환이 실패해도 마크다운은 이미 남아 있다
        print(f"  워드 변환 실패: {e}")
        out = md_path
    print(f"✓ 문서 생성: {out}")
    return out


def main():
    stamp = datetime.datetime.now()
    tracks = open_tracks()
    for t in tracks:
        t.start()
    time.sleep(0.5)

    live = []
    for t in tracks:
        if t.error:
            print(f"  {t.label} 실패: {t.error}")
        else:
            live.append(t)
    if not live:
        sys.exit("녹음 장치를 하나도 못 열었습니다.")

    # 바탕화면에는 결과 문서 하나만 남긴다. wav/txt/md 중간 파일은 임시 폴더에.
    work = os.path.join(tempfile.gettempdir(), "회의록")
    os.makedirs(work, exist_ok=True)
    wav_path = os.path.join(work, stamp.strftime("회의록_%Y-%m-%d_%H%M") + ".wav")
    # API 전사는 녹음 뒤에 한 번에 올리는 게 빠르므로 로컬일 때만 미리 전사한다.
    transcriber = LiveTranscriber(live, wav_path) if STT == "local" else None
    if transcriber:
        transcriber.start()

    print(f"\n● 녹음 중 [{', '.join(t.label for t in live)}]. 회의 끝나면 Enter.")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    for t in live:
        t.stopping.set()
    for t in live:
        t.join(timeout=3)
    if transcriber:
        transcriber.stopping.set()
        print("\n  진행 중인 전사 덩어리 마무리…")
        transcriber.join()

    audio = mix(*(t.mono() for t in live))
    secs = len(audio) / SR
    if secs < 3:
        sys.exit("녹음된 소리가 없습니다.")
    dur = f"{secs:.0f}초" if secs < 60 else f"{secs / 60:.0f}분"
    write_wav(wav_path, audio)
    print(f"■ 녹음 종료 ({dur}).")

    out = report(wav_path)
    final = os.path.join(desktop(), os.path.basename(out))
    shutil.move(out, final)
    print(f"✓ 바탕화면: {os.path.basename(final)}   (원본 녹음: {work})")
    os.startfile(final)


def selftest():
    a = np.array([1.0, 1.0, 1.0], "float32")
    b = np.array([1.0], "float32")
    m = mix(a, b)
    assert len(m) == 3, m
    assert abs(m[0] - 0.95) < 1e-6, m       # 2.0 피크 -> 0.95로 정규화
    assert abs(m[1] - 0.475) < 1e-6, m      # 패딩된 뒷부분은 절반
    assert len(mix()) == 0
    q = np.array([0.5, -0.5], "float32")
    assert np.allclose(mix(q), q)           # 피크 <= 1 이면 그대로
    assert os.path.isdir(desktop()), desktop()

    x = np.ones(SR * 10, "float32")
    x[SR * 7:SR * 7 + SR // 10] = 0                       # 7초 지점만 조용함
    assert abs(quiet_cut(x) / SR - 7.05) < 0.01, quiet_cut(x) / SR
    assert next_start(0.0, 28.0, None) == 25.0            # 말 없으면 3초만 겹쳐 다음으로
    assert next_start(0.0, 28.0, 27.5) == 27.5            # 마지막 문장 끝부터
    assert next_start(30.0, 58.0, 41.0) == 55.0           # 3초 넘게 되돌아가지 않음

    assert by_speaker([(1, "안녕"), (1, "하세요"), (2, "네"), (1, " "), (1, "시작")]) == \
        "화자1: 안녕 하세요\n화자2: 네\n화자1: 시작"

    t = Track(None, "가짜")
    t.frames = [np.arange(i * 4, i * 4 + 4, dtype="float32") for i in range(5)]
    assert t.length() == 20
    assert t.segment(6, 13).tolist() == list(range(6, 13)), t.segment(6, 13)
    assert t.segment(18, 40).tolist() == [18, 19]

    part = os.path.join(os.environ["TEMP"], "_mt.txt.part")
    assert resume_from(part + ".nope") == ([], 0.0)          # 파일 없으면 처음부터
    with open(part, "w", encoding="utf-8") as f:
        f.write("12.50\t안녕하세요\n\n30.00\t네 그럼 시작\t하죠\n")
    assert resume_from(part) == (["안녕하세요", "네 그럼 시작\t하죠"], 30.0), resume_from(part)
    os.remove(part)

    md = os.path.join(os.environ["TEMP"], "_mt.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("# 제목\n\n## 절\n\n- 항목 **강조**\n\n---\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n본문\n")
    dx = to_docx(md)
    from docx import Document
    texts = [p.text for p in Document(dx).paragraphs]
    assert "제목" in texts and "절" in texts, texts
    assert "항목 강조" in texts, texts                    # ** 제거
    assert "a   b" in texts and "1   2" in texts, texts    # 표 행 보존
    assert "본문" in texts, texts
    assert not [t for t in texts if t and set(t) <= set("-| ")], texts  # 구분선 제거
    os.remove(md)
    os.remove(dx)

    tracks = open_tracks()
    assert tracks, "입력 장치를 하나도 못 찾음"
    for t in tracks:
        t.start()
    time.sleep(1.5)
    for t in tracks:
        t.stopping.set()
    for t in tracks:
        t.join(timeout=3)
        print(f"  {t.label}: {'실패 ' + str(t.error) if t.error else f'{len(t.mono())/SR:.2f}초'}")
    assert any(t.error is None and len(t.mono()) for t in tracks), "녹음된 샘플이 없음"
    print("ok")


if __name__ == "__main__":
    wavs = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--selftest" in sys.argv:
        selftest()
    elif wavs:
        for p in wavs:
            report(p)
    else:
        main()
