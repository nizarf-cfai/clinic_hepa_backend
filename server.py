import asyncio
import os
import json
import base64
import contextlib
import logging
import datetime
import threading
import copy
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from dotenv import load_dotenv

# --- Local Modules ---
import question_manager
import diagnosis_manager

import google.auth.transport.requests
import google.auth.transport.grpc 

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medforce-backend")

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration ---
VOICE_MODEL = "gemini-live-2.5-flash-preview-native-audio-09-2025"
ADVISOR_MODEL = "gemini-2.5-flash-lite" 
DIAGNOSER_MODEL = "gemini-2.5-flash-lite" 
RANKER_MODEL = "gemini-2.5-flash-lite" 

# --- LOAD STATIC DATA ---
try:
    with open("questions.json", 'r') as file:
        QUESTION_LIST = json.load(file)
    with open("patient_profile/arthur_info.md", "r", encoding="utf-8") as f:
        PATIENT_PROFILE_TEXT = f.read()
    with open("patient_profile/nurse.md", "r", encoding="utf-8") as f:
        NURSE_PROMPT = f.read()
    with open("patient_profile/arthur.md", "r", encoding="utf-8") as f:
        PATIENT_PROMPT = f.read()
    
    COMPLETION_CHECKLIST = [
        "Chief Complaint identified",
        "Pain Level (severity) quantified",
        "Duration of symptoms established",
        "Current Medications listed",
        "Drug Allergies listed",
        "Past Surgeries listed"
    ]
except Exception as e:
    logger.error(f"Failed to load static files: {e}")
    QUESTION_LIST = []
    PATIENT_PROFILE_TEXT = ""
    NURSE_PROMPT = "You are a nurse."
    PATIENT_PROMPT = "You are a patient."

# ==========================================
# LOGIC AGENTS (Ported from live_session_manager2)
# ==========================================

class QuestionRankingAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))
        self.response_schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"rank": { "type": "INTEGER" }, "qid": { "type": "STRING" }}, "required": ["rank", "qid"]}}
        try:
            with open("patient_profile/q_ranker.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Rank by priority."

    async def rank_questions(self, conversation_history, current_diagnosis, q_list):
        prompt = f"Patient Profile:\n{PATIENT_PROFILE_TEXT}\n\nHistory:\n{json.dumps(conversation_history)}\n\nDiagnosis:\n{json.dumps(current_diagnosis)}\n\nQuestions:\n{json.dumps(q_list)}"
        try:
            response = await self.client.aio.models.generate_content(
                model=RANKER_MODEL, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.1)
            )
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Ranker Error: {e}")
            return [{"rank": i+1, "qid": q["qid"]} for i, q in enumerate(q_list)]

class DiagnosisTriggerAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))
        self.response_schema = {"type": "OBJECT", "properties": {"should_run": { "type": "BOOLEAN" }, "reason": { "type": "STRING" }}, "required": ["should_run", "reason"]}
        try:
            with open("patient_profile/diagnosis_trigger.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Return true if new info."

    async def check_trigger(self, conversation_history):
        if not conversation_history: return False, "Empty"
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", contents=f"History:\n{json.dumps(conversation_history)}",
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.0)
            )
            res = json.loads(response.text)
            return res.get("should_run", False), res.get("reason", "")
        except: return True, "Fallback"

class DiagnoseEvaluatorAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))
        self.response_schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"diagnosis": { "type": "STRING" }, "did": { "type": "STRING" }, "indicators_point": { "type": "ARRAY", "items": { "type": "STRING" } }}, "required": ["diagnosis", "did", "indicators_point"]}}
        try:
            with open("patient_profile/diagnosis_eval.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Merge diagnoses."

    async def evaluate_diagnoses(self, diagnosis_pool, new_diagnosis_list, interview_data):
        prompt = f"Context:\n{json.dumps(interview_data)}\n\nMaster Pool:\n{json.dumps(diagnosis_pool)}\n\nNew Candidates:\n{json.dumps(new_diagnosis_list)}"
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.1)
            )
            return json.loads(response.text)
        except: return diagnosis_pool + new_diagnosis_list

class DiagnoseAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))
        self.response_schema = {"type": "OBJECT", "properties": {"diagnosis_list": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"diagnosis": { "type": "STRING" }, "did": { "type": "STRING" }, "indicators_point": { "type": "ARRAY", "items": { "type": "STRING" } }}, "required": ["diagnosis", "indicators_point", "did"]}}, "follow_up_questions": {"type": "ARRAY", "items": { "type": "STRING" }}}, "required": ["diagnosis_list", "follow_up_questions"]}
        try:
            with open("patient_profile/diagnoser.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Diagnose patient."

    async def get_diagnosis_update(self, interview_data, current_diagnosis_hypothesis):
        prompt = f"Patient:\n{PATIENT_PROFILE_TEXT}\n\nTranscript:\n{json.dumps(interview_data)}\n\nState:\n{json.dumps(current_diagnosis_hypothesis)}"
        try:
            response = await self.client.aio.models.generate_content(
                model=DIAGNOSER_MODEL, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.2)
            )
            res = json.loads(response.text)
            return {"diagnosis_list": res.get("diagnosis_list", []), "follow_up_questions": res.get("follow_up_questions", [])}
        except: return {"diagnosis_list": current_diagnosis_hypothesis, "follow_up_questions": []}

class AdvisorAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))
        self.response_schema = {"type": "OBJECT", "properties": {"question": { "type": "STRING" }, "qid": { "type": "STRING" }, "end_conversation": { "type": "BOOLEAN" }, "reasoning": { "type": "STRING" }}, "required": ["question", "end_conversation", "reasoning", "qid"]}
        try:
            with open("patient_profile/advisor_agent.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Advise nurse."

    async def get_advise(self, conversation_history, q_list):
        prompt = f"Context:\n{PATIENT_PROFILE_TEXT}\n\nHistory:\n{json.dumps(conversation_history)}\n\nChecklist:\n{json.dumps(COMPLETION_CHECKLIST)}\n\nQuestions:\n{json.dumps(q_list)}"
        try:
            response = await self.client.aio.models.generate_content(
                model=ADVISOR_MODEL, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.2)
            )
            res = json.loads(response.text)
            return res.get("question"), res.get("reasoning"), res.get("end_conversation"), res.get("qid")
        except: return "Continue.", "Error", False, None

class AnswerHighlighterAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))
        self.response_schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"level": { "type": "STRING", "enum": ["danger", "warning"] }, "text": { "type": "STRING" }}, "required": ["level", "text"]}}
        try:
            with open("patient_profile/highlight_agent.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Extract keywords."

    async def highlight_text(self, patient_answer: str, diagnosis_list: list):
        if not patient_answer or len(patient_answer) < 3: return []
        prompt = f"Context:\n{json.dumps(diagnosis_list)}\n\nAnswer:\n\"{patient_answer}\""
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.0)
            )
            return json.loads(response.text)
        except: return []

# ==========================================
# THREADING & HISTORY
# ==========================================

class TranscriptManager:
    def __init__(self):
        self.history = []
    
    def log(self, speaker, text, highlight_data=None):
        entry = {"timestamp": datetime.datetime.now().strftime("%H:%M:%S"), "speaker": speaker, "text": text.strip()}
        if speaker == "PATIENT": entry["highlight"] = highlight_data or []
        self.history.append(entry)
        logger.info(f"📝 {speaker}: {text[:50]}...")
    
    def get_history(self):
        return self.history

class ClinicalLogicThread(threading.Thread):
    def __init__(self, transcript_manager, qm, diagnosis_manager, shared_state):
        super().__init__()
        self.tm = transcript_manager
        self.qm = qm
        self.dm = diagnosis_manager
        self.shared_state = shared_state
        self.running = True
        self.daemon = True 

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        self.trigger_agent = DiagnosisTriggerAgent()
        self.diagnoser = DiagnoseAgent()
        self.evaluator = DiagnoseEvaluatorAgent()
        self.ranker = QuestionRankingAgent()

        logger.info("🩺 Logic Thread Started")
        loop.run_until_complete(self._monitor_loop())

    async def _monitor_loop(self):
        while self.running:
            try:
                current_cycle = self.shared_state.get("cycle", 0)
                # Run logic every 3rd turn to simulate periodic review
                if (current_cycle % 3) == 0:
                    history = copy.deepcopy(self.tm.get_history())
                    should_run, reason = await self.trigger_agent.check_trigger(history)

                    if should_run:
                        logger.info(f"⚡ Diagnosis Triggered: {reason}")
                        
                        # 1. Diagnose
                        diag_res = await self.diagnoser.get_diagnosis_update(history, self.dm.get_diagnosis_basic())
                        self.dm.update_diagnoses(diag_res.get("diagnosis_list"))
                        
                        # 2. Evaluate & Merge
                        merged_diag = await self.evaluator.evaluate_diagnoses(
                            self.dm.get_consolidated_diagnoses_basic(),
                            diag_res.get("diagnosis_list"), 
                            history
                        )
                        self.dm.set_consolidated_diagnoses(merged_diag)
                        
                        # 3. Update Questions
                        self.qm.add_questions_from_text(diag_res.get("follow_up_questions"))
                        diag_stream = self.dm.get_consolidated_diagnoses()
                        q_list = self.qm.get_recommend_question()
                        
                        # 4. Rank
                        ranked_q = await self.ranker.rank_questions(history, diag_stream, q_list)
                        self.qm.update_ranking(ranked_q)

                        # 5. Update State for Frontend
                        self.shared_state["ranked_questions"] = self.qm.get_recommend_question()
                        self.shared_state["diagnosis_data"] = diag_stream
                        self.shared_state["data_ready"] = True
                        
            except Exception as e:
                logger.error(f"Logic Thread Error: {e}")
            
            await asyncio.sleep(2)

    def stop(self):
        self.running = False

# ==========================================
# VOICE AGENT & ORCHESTRATOR
# ==========================================

class TextBridgeAgent:
    def __init__(self, name, system_instruction, voice_name):
        self.name = name
        self.system_instruction = system_instruction
        self.voice_name = voice_name
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))
        self.session = None

    def get_connection_context(self):
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"], 
            system_instruction=types.Content(parts=[types.Part(text=self.system_instruction)]),
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name))),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        return self.client.aio.live.connect(model=VOICE_MODEL, config=config)

    def set_session(self, session):
        self.session = session

    async def speak_and_stream(self, text_input, websocket: WebSocket):
        if not self.session: return None
        await self.session.send(input=text_input, end_of_turn=True)
        text_accumulator = []
        
        try:
            async for response in self.session.receive():
                if data := response.data:
                    b64_audio = base64.b64encode(data).decode('utf-8')
                    await websocket.send_json({"type": "audio", "speaker": self.name, "data": b64_audio})
                    await asyncio.sleep(0.01)

                if response.server_content and response.server_content.output_transcription:
                    if text := response.server_content.output_transcription.text:
                        text_accumulator.append(text)

                if response.server_content and response.server_content.turn_complete:
                    full_text = "".join(text_accumulator).strip()
                    if full_text:
                        # We send transcript here, but the Manager handles logging
                        await websocket.send_json({"type": "transcript", "speaker": self.name, "text": full_text})
                        return full_text
                    return "[...]"
            return None
        except Exception as e:
            logger.error(f"Stream Error ({self.name}): {e}")
            return None

class SimulationManager:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.nurse = TextBridgeAgent("NURSE", NURSE_PROMPT, "Aoede")
        self.patient = TextBridgeAgent("PATIENT", PATIENT_PROMPT, "Puck")
        self.advisor = AdvisorAgent()
        self.highlighter = AnswerHighlighterAgent()
        
        self.tm = TranscriptManager()
        self.qm = question_manager.QuestionPoolManager(QUESTION_LIST)
        self.dm = diagnosis_manager.DiagnosisManager()
        
        self.cycle = 0
        self.shared_state = {
            "ranked_questions": self.qm.get_recommend_question(),
            "diagnosis_data": [],
            "cycle": 0,
            "data_ready": False
        }
        self.running = False

    async def run(self):
        self.running = True
        await self.websocket.send_json({"type": "system", "message": "Initializing..."})

        # Start Logic Thread
        logic_thread = ClinicalLogicThread(self.tm, self.qm, self.dm, self.shared_state)
        logic_thread.start()

        async with contextlib.AsyncExitStack() as stack:
            self.nurse.set_session(await stack.enter_async_context(self.nurse.get_connection_context()))
            self.patient.set_session(await stack.enter_async_context(self.patient.get_connection_context()))
            await self.websocket.send_json({"type": "system", "message": "Connected. Starting Assessment."})

            next_instruction = "Introduce yourself and ask for Name and DOB."
            patient_last_words = "Hello."
            interview_end = False

            while self.running:
                self.shared_state["cycle"] = self.cycle 

                # --- 1. NURSE TURN ---
                nurse_input = f"Patient said: '{patient_last_words}'\n[SUPERVISOR: {next_instruction}]"
                nurse_text = await self.nurse.speak_and_stream(nurse_input, self.websocket)
                
                if not nurse_text:
                    nurse_text = "[The nurse waits]"
                
                self.tm.log("NURSE", nurse_text)

                if "doctor" in next_instruction.lower() and "bye" in nurse_text.lower():
                    await self.websocket.send_json({"type": "system", "message": "Ended by Protocol."})
                    break

                await asyncio.sleep(0.5)

                # --- 2. PATIENT TURN ---
                patient_text = await self.patient.speak_and_stream(nurse_text, self.websocket)
                
                highlight_result = []
                if patient_text:
                    patient_last_words = patient_text
                    # Highlight analysis
                    highlight_result = await self.highlighter.highlight_text(patient_text, self.dm.get_consolidated_diagnoses_basic())
                    # Send highlights to frontend
                    if highlight_result:
                        await self.websocket.send_json({"type": "highlights", "data": highlight_result})
                else:
                    patient_text = "[The patient nods]"
                    patient_last_words = "(Silent)"

                self.tm.log("PATIENT", patient_text, highlight_data=highlight_result)
                await asyncio.sleep(0.5)

                if interview_end: break

                # --- 3. ADVISOR & DATA SYNC ---
                try:
                    # Check if thread updated data
                    if self.shared_state.get("data_ready"):
                        await self.websocket.send_json({"type": "diagnosis", "data": self.shared_state["diagnosis_data"]})
                        await self.websocket.send_json({"type": "questions", "data": self.qm.get_questions()}) # Send full list for UI
                        self.shared_state["data_ready"] = False

                    # Advisor decides next move (fast logic)
                    current_ranked = self.shared_state["ranked_questions"]
                    question, reasoning, status, qid = await self.advisor.get_advise(self.tm.get_history(), current_ranked)
                    
                    if qid: self.qm.update_status(qid, "asked")
                    
                    await self.websocket.send_json({"type": "system", "message": f"Logic: {reasoning}"})
                    
                    next_instruction = question
                    interview_end = status
                    self.cycle += 1

                except Exception as e:
                    logger.error(f"Main Loop Logic Error: {e}")
                    next_instruction = "Continue assessment."

                if self.websocket.client_state.name == "DISCONNECTED": break

        logic_thread.stop()

@app.websocket("/ws/simulation")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    manager = SimulationManager(websocket)
    try:
        data = await websocket.receive_text()
        if data == "start":
            await manager.run()
    except WebSocketDisconnect:
        manager.running = False
        if hasattr(manager, 'logic_thread'): manager.logic_thread.stop()