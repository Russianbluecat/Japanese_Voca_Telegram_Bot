#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
텔레그램 일본어 단어 퀴즈봇 (다중 사용자 지원 버전)

동작 방식:
1. 구글시트(Raw_Data 탭)에서 CSV로 단어 데이터를 읽어온다.
2. Blank_Sentence 열에서 괄호 안 단어를 정답으로 추출한다.
3. 등록된 모든 chat_id 각각에게 랜덤 5문제(4지선다)를 만들어 "동시에" 보낸다.
4. 한 번의 폴링 루프 안에서 모든 사용자의 응답/타임아웃을 독립적으로 처리한다.
   (한 사람이 3번 문제에서 고민 중이어도, 다른 사람은 이미 4번 문제를 받을 수 있음)
5. 각자 5문제가 끝나면 그 사람에게만 "몇 개 맞혔다" 결과를 보낸다.

이 스크립트는 "한 번 실행되면 등록된 모두의 퀴즈가 끝날 때까지 진행 후 종료"되는 구조이며,
GitHub Actions 같은 스케줄러로 매일 밤 9시에 한 번씩 실행시키는 것을 전제로 한다.
"""

import os
import re
import csv
import io
import time
import random
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")  # 한국 표준시

# ── 환경변수로부터 설정값 읽기 (GitHub Actions Secrets에서 주입됨) ──
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# 여러 명을 지원하기 위해 콤마(,)로 구분된 chat_id 목록을 받는다.
# 예: "79372278,123456789,987654321"
# 한 명만 쓸 경우 그냥 "79372278" 하나만 넣어도 된다.
CHAT_IDS_RAW = os.environ["TELEGRAM_CHAT_IDS"]
CHAT_IDS = [cid.strip() for cid in CHAT_IDS_RAW.split(",") if cid.strip()]

# 구글시트 정보
SHEET_ID = "1F-MzECDTU_S_6lR6_FJLpXOAih4Kg1SjHTCUeqgzOqo"
SHEET_NAME = "Raw_Data"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={SHEET_NAME}"

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

NUM_QUESTIONS = 5          # 한 사람당 낼 문제 수
NUM_CHOICES = 4            # 선택지 개수 (정답 포함)
ANSWER_TIMEOUT_SEC = 600   # 문제 하나당 답변 대기 시간 (10분)
LONG_POLL_TIMEOUT_SEC = 10 # 텔레그램 getUpdates 롱폴 대기 시간 (타임아웃 체크 주기에 영향)


# ────────────────────────────────
# 1. 구글시트에서 문제 데이터 가져오기
# ────────────────────────────────
def load_questions_from_sheet():
    """
    구글시트 CSV를 읽어서 (문제문장, 정답단어) 쌍의 리스트를 반환한다.
    Blank_Sentence 열 예시: "毎朝 元気に(あいさつ)をします。"
    -> 정답: "あいさつ"
    -> 문제문장(빈칸 처리): "毎朝 元気に(　　　)をします。"
    """
    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # 구글시트 CSV는 UTF-8. (엑셀에서 열면 깨져 보이는 것과 무관, 코드는 정상 처리됨)

    reader = csv.DictReader(io.StringIO(resp.text))

    questions = []
    pattern = re.compile(r"\(([^()]+)\)")  # 괄호 안 내용을 추출하는 정규식

    for row in reader:
        blank_sentence = (row.get("Blank_Sentence") or "").strip()
        if not blank_sentence:
            continue  # 빈 행은 건너뜀

        match = pattern.search(blank_sentence)
        if not match:
            continue  # 괄호가 없는 행은 건너뜀

        answer = match.group(1).strip()
        if not answer:
            continue

        translation = (row.get("Translation") or "").strip()

        # 정답 부분을 빈칸(전각 공백 3칸)으로 치환해서 문제 문장을 만든다
        question_text = blank_sentence[:match.start()] + "(　　　)" + blank_sentence[match.end():]

        questions.append({
            "question": question_text,
            "translation": translation,
            "answer": answer,
        })

    return questions


# ────────────────────────────────
# 2. 텔레그램 API 헬퍼 함수들
# ────────────────────────────────
def send_message(chat_id, text, reply_markup=None):
    """
    메시지 전송. 실패해도(예: 상대가 봇을 차단/채팅방 나감) 예외를 던지지 않고
    None을 반환한다. 이렇게 해야 한 사람에게 보내기 실패해도
    다른 사람들의 퀴즈 진행에는 영향을 주지 않는다.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            print(f"[경고] chat_id={chat_id} 에게 메시지 전송 실패: {data.get('description')}")
            return None
        return data
    except requests.RequestException as e:
        print(f"[경고] chat_id={chat_id} 전송 중 네트워크 오류: {e}")
        return None


def answer_callback_query(callback_query_id, text=None):
    """버튼 누른 사람에게 로딩 스피너를 없애주는 용도 (선택 사항이지만 UX상 넣어줌)"""
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(f"{API_BASE}/answerCallbackQuery", json=payload, timeout=30)


def get_updates(offset, timeout=LONG_POLL_TIMEOUT_SEC):
    """
    롱폴링으로 새 업데이트(버튼 클릭 등)를 가져온다.
    offset: 이 값 이후의 update_id만 가져옴 (중복 처리 방지)
    이 봇 전체에 온 모든 사용자의 업데이트를 한 번에 가져와서,
    이후 로직에서 chat_id별로 분류(라우팅)한다.
    """
    params = {"timeout": timeout, "offset": offset}
    resp = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=timeout + 10)
    resp.raise_for_status()
    return resp.json().get("result", [])


def build_inline_keyboard(choices):
    """
    choices: ["あいさつ", "呼ばれ", "名前", "返事"] 같은 리스트
    -> 텔레그램 인라인 키보드(4개 버튼, 세로 1열) 형태로 변환

    주의: callback_data는 텔레그램 규격상 최대 64바이트 제한이 있다.
    혹시 단어가 아주 길어지는 경우를 대비해, 화면에 보이는 버튼 글자(text)는
    단어 원문 그대로 두되, 내부적으로 주고받는 callback_data는 "0", "1", "2", "3" 같은
    짧은 인덱스 번호로만 보낸다. (실제 선택된 단어는 session["current_choices"]에서
    인덱스로 다시 찾아온다.)
    """
    keyboard = [[{"text": choice, "callback_data": str(i)}] for i, choice in enumerate(choices)]
    return {"inline_keyboard": keyboard}


# ────────────────────────────────
# 3. 사용자별 퀴즈 진행 상태(세션) 관리
# ────────────────────────────────
def make_session(questions, answer_pool):
    """한 사용자를 위한 세션(진행 상태)을 만든다."""
    return {
        "questions": random.sample(questions, NUM_QUESTIONS),
        "answer_pool": answer_pool,
        "index": 0,        # 현재 몇 번째 문제인지 (0부터 시작)
        "score": 0,
        "waiting": False,  # 현재 답변을 기다리는 중인지
        "deadline": None,  # 이 시각까지 답 안 오면 타임아웃
        "done": False,
    }


def build_choices(session):
    """현재 문제에 대한 4지선다 선택지를 만든다."""
    current_q = session["questions"][session["index"]]
    distractor_pool = [a for a in session["answer_pool"] if a != current_q["answer"]]
    distractors = random.sample(distractor_pool, min(NUM_CHOICES - 1, len(distractor_pool)))
    choices = distractors + [current_q["answer"]]
    random.shuffle(choices)
    return choices


def send_next_question(chat_id, session):
    """
    세션의 현재 index에 해당하는 문제를 전송하고, 대기 상태로 전환한다.
    전송에 실패하면(차단/채팅방 나감 등) 이 사람의 세션을 조용히 종료 처리한다.
    """
    q = session["questions"][session["index"]]
    choices = build_choices(session)
    session["current_choices"] = choices

    translation_line = f"({q['translation']})\n" if q["translation"] else ""

    question_text = (
        f"[{session['index'] + 1}/{NUM_QUESTIONS}] 문제\n\n"
        f"{q['question']}\n"
        f"{translation_line}\n"
        f"정답 단어를 골라주세요 👇"
    )
    result = send_message(chat_id, question_text, reply_markup=build_inline_keyboard(choices))

    if result is None:
        # 이 사람에게는 더 이상 보낼 수 없으므로 세션을 끝난 것으로 처리하고
        # 나머지 사람들의 진행에는 영향을 주지 않는다.
        session["waiting"] = False
        session["done"] = True
        return

    session["waiting"] = True
    session["deadline"] = time.time() + ANSWER_TIMEOUT_SEC


def advance_session(chat_id, session):
    """현재 문제를 마치고 다음 문제로 넘어가거나, 끝났으면 최종 결과를 보낸다."""
    session["index"] += 1
    session["waiting"] = False

    if session["index"] >= NUM_QUESTIONS:
        session["done"] = True
        send_message(
            chat_id,
            f"🎉 퀴즈 종료! {NUM_QUESTIONS}문제 중 {session['score']}개 맞혔습니다."
        )
    else:
        send_next_question(chat_id, session)


def handle_answer(chat_id, session, selected):
    """사용자가 버튼을 눌렀을 때 정답 여부를 판정하고 알려준다."""
    q = session["questions"][session["index"]]
    is_correct = (selected == q["answer"])

    if is_correct:
        session["score"] += 1
        send_message(chat_id, f"✅ 정답입니다! ('{selected}')")
    else:
        send_message(chat_id, f"❌ 오답입니다. 선택: '{selected}' / 정답: '{q['answer']}'")

    advance_session(chat_id, session)


def handle_timeout(chat_id, session):
    """시간 내 응답이 없을 때 처리."""
    q = session["questions"][session["index"]]
    send_message(chat_id, f"⏰ 시간 초과! 정답은 '{q['answer']}' 였습니다.")
    advance_session(chat_id, session)


# ────────────────────────────────
# 4. 메인 실행 흐름
# ────────────────────────────────
def main():
    if not CHAT_IDS:
        raise RuntimeError("TELEGRAM_CHAT_IDS 환경변수에 최소 1개의 chat_id가 필요합니다.")

    all_questions = load_questions_from_sheet()
    if len(all_questions) < NUM_QUESTIONS:
        for cid in CHAT_IDS:
            send_message(cid, "⚠️ 문제 데이터가 부족합니다. 구글시트를 확인해주세요.")
        return

    answer_pool = [q["answer"] for q in all_questions]

    today_str = datetime.now(KST).strftime("%Y년 %m월 %d일")

    # 사용자별 세션 생성 + 시작 메시지 + 첫 문제 전송
    sessions = {}
    for cid in CHAT_IDS:
        session = make_session(all_questions, answer_pool)
        sessions[cid] = session

        start_result = send_message(
            cid, f"🇯🇵 {today_str} 오늘의 일본어 단어 퀴즈 시작! (총 {NUM_QUESTIONS}문제)"
        )
        if start_result is None:
            # 시작 메시지조차 못 보내면 (차단 등) 이 사람은 건너뛴다
            session["done"] = True
            continue

        send_next_question(cid, session)

    # 시작 전, 혹시 밀려있던 이전 업데이트들은 무시하도록 offset을 최신으로 맞춰준다
    last_update_id = 0
    initial_updates = get_updates(offset=-1, timeout=1)
    if initial_updates:
        last_update_id = initial_updates[-1]["update_id"]

    # 모든 사용자가 끝날 때까지 반복
    while any(not s["done"] for s in sessions.values()):
        updates = get_updates(offset=last_update_id + 1)

        for update in updates:
            last_update_id = max(last_update_id, update["update_id"])

            callback = update.get("callback_query")
            if not callback:
                continue

            cid = str(callback["message"]["chat"]["id"])
            session = sessions.get(cid)
            if session is None or session["done"] or not session["waiting"]:
                continue  # 등록되지 않은 사람이거나, 이미 끝났거나, 대기 중이 아니면 무시

            selected_index_raw = callback["data"]
            answer_callback_query(callback["id"])  # 버튼 로딩 스피너 제거

            try:
                selected_index = int(selected_index_raw)
                selected = session["current_choices"][selected_index]
            except (ValueError, IndexError):
                continue  # 예상 못한 데이터면 무시 (안전장치)

            handle_answer(cid, session, selected)

        # 타임아웃된 세션 처리 (버튼을 안 누르고 10분이 지난 경우)
        now = time.time()
        for cid, session in sessions.items():
            if session["waiting"] and not session["done"] and now > session["deadline"]:
                handle_timeout(cid, session)


if __name__ == "__main__":
    main()
