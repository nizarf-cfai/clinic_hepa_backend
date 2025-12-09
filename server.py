import asyncio
import os
import json
import base64
import contextlib
import logging
import datetime
import threading
import copy
import uuid 
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException # Add HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel # Add Pydantic for request body
from google.cloud import storage # Add Storage Client
from fastapi.responses import JSONResponse, Response

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


class PatientFileRequest(BaseModel):
    pid: str       # e.g., "p001"
    file_name: str # e.g., "lab_results.png" or "history.md"


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
# LOGIC AGENTS
# ==========================================

class BaseLogicAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))

class QuestionRankingAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
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

class DiagnosisTriggerAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
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

class DiagnoseEvaluatorAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
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

class DiagnoseAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
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

class AdvisorAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
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

class AnswerHighlighterAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
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
# THREADING & HISTORY MANAGER
# ==========================================

class TranscriptManager:
    def __init__(self):
        self.history = []
        self._lock = threading.Lock()
    
    def log(self, speaker, text, highlight_data=None):
        with self._lock:
            entry = {"timestamp": datetime.datetime.now().strftime("%H:%M:%S"), "speaker": speaker, "text": text.strip()}
            if speaker == "PATIENT": entry["highlight"] = highlight_data or []
            self.history.append(entry)
            logger.info(f"📝 {speaker}: {text[:50]}...")
    
    def get_history(self):
        with self._lock:
            return copy.deepcopy(self.history)

class ClinicalLogicThread(threading.Thread):
    def __init__(self, transcript_manager, qm, dm, shared_state, main_loop, websocket):
        super().__init__()
        self.tm = transcript_manager
        self.qm = qm
        self.dm = dm
        self.shared_state = shared_state
        self.main_loop = main_loop 
        self.websocket = websocket
        
        self.running = True
        self.daemon = True 
        self.last_processed_count = 0

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        self.trigger = DiagnosisTriggerAgent()
        self.diagnoser = DiagnoseAgent()
        self.evaluator = DiagnoseEvaluatorAgent()
        self.ranker = QuestionRankingAgent()

        logger.info("🩺 Logic Thread Started")
        loop.run_until_complete(self._monitor_loop())

    async def _push_update(self, type_str, data):
        if self.websocket and self.main_loop and not self.websocket.client_state.name == "DISCONNECTED":
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.websocket.send_json({"type": type_str, "data": data}),
                    self.main_loop
                )
                future.result(timeout=1)
            except Exception:
                pass

    async def _monitor_loop(self):
        # NOTE: Initial Logic removed from here. It is now in SimulationManager.run()
        while self.running:
            try:
                history = self.tm.get_history()
                current_len = len(history)

                # TRIGGER CONDITION: Has history grown?
                if current_len > self.last_processed_count:
                    
                    logger.info(f"⚡ New Transcript Detected ({current_len} turns). Running Logic...")
                    
                    # 1. Diagnose
                    diag_res = await self.diagnoser.get_diagnosis_update(history, self.dm.get_diagnosis_basic())
                    self.dm.update_diagnoses(diag_res.get("diagnosis_list"))
                    
                    # 2. Evaluate
                    merged_diag = await self.evaluator.evaluate_diagnoses(
                        self.dm.get_consolidated_diagnoses_basic(),
                        diag_res.get("diagnosis_list"), 
                        history
                    )
                    self.dm.set_consolidated_diagnoses(merged_diag)
                    
                    # 3. Questions
                    self.qm.add_questions_from_text(diag_res.get("follow_up_questions"))
                    
                    # 4. Rank
                    diag_stream = self.dm.get_consolidated_diagnoses()
                    q_list = self.qm.get_recommend_question()
                    ranked_q = await self.ranker.rank_questions(history, diag_stream, q_list)
                    self.qm.update_ranking(ranked_q)

                    # 5. Push
                    await self._push_update("diagnosis", diag_stream)
                    await self._push_update("questions", self.qm.get_questions())
                    
                    self.shared_state["ranked_questions"] = self.qm.get_recommend_question()
                    
                    # Update checkpoint
                    self.last_processed_count = current_len
                    logger.info("✅ Logic Cycle Complete")

            except Exception as e:
                logger.error(f"Logic Thread Error: {e}")
            
            await asyncio.sleep(1)

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

    async def speak_and_stream(self, text_input, websocket: WebSocket, highlighter=None, diagnosis_context=None):
        if not self.session: return None, []
        
        try:
            await self.session.send(input=text_input, end_of_turn=True)
        except Exception:
            return None, []

        turn_id = str(uuid.uuid4())
        text_accumulator = []
        
        try:
            async for response in self.session.receive():
                if data := response.data:
                    b64_audio = base64.b64encode(data).decode('utf-8')
                    await websocket.send_json({"type": "audio", "id": turn_id, "speaker": self.name, "data": b64_audio})
                    await asyncio.sleep(0.01)

                if response.server_content and response.server_content.output_transcription:
                    if text := response.server_content.output_transcription.text:
                        text_accumulator.append(text)

                if response.server_content and response.server_content.turn_complete:
                    full_text = "".join(text_accumulator).strip()
                    if full_text:
                        highlights = []
                        if highlighter and diagnosis_context:
                            try:
                                highlights = await highlighter.highlight_text(full_text, diagnosis_context)
                            except: pass

                        await websocket.send_json({
                            "type": "transcript",
                            "id": turn_id,
                            "speaker": self.name,
                            "text": full_text,
                            "highlights": highlights
                        })
                        return full_text, highlights
                    return "[...]", []
            return None, []
        except Exception as e:
            logger.error(f"Stream Error ({self.name}): {e}")
            return None, []

class SimulationManager:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        
        # Voice Agents
        self.nurse = TextBridgeAgent("NURSE", NURSE_PROMPT, "Aoede")
        self.patient = TextBridgeAgent("PATIENT", PATIENT_PROMPT, "Puck")
        
        # Logic Agents (Instantiated here for Init phase)
        self.advisor = AdvisorAgent()
        self.highlighter = AnswerHighlighterAgent()
        self.diagnoser = DiagnoseAgent()
        self.evaluator = DiagnoseEvaluatorAgent()
        self.ranker = QuestionRankingAgent()
        
        self.tm = TranscriptManager()
        self.qm = question_manager.QuestionPoolManager(copy.deepcopy(QUESTION_LIST))
        self.dm = diagnosis_manager.DiagnosisManager()
        
        self.cycle = 0
        self.shared_state = {
            "ranked_questions": self.qm.get_recommend_question(),
            "cycle": 0
        }
        self.running = False

    async def run(self):
        self.running = True
        await self.websocket.send_json({"type": "system", "message": "Initializing Agents..."})

        # --- INITIALIZATION PHASE (Running on Main Thread BEFORE loop) ---
        try:
            logger.info("⚡ Running Initial Diagnosis (Main Thread)...")
            initial_history = [{"speaker": "PATIENT_INFO", "text": PATIENT_PROFILE_TEXT}]

            # 1. Diagnose
            diag_res = await self.diagnoser.get_diagnosis_update(initial_history, self.dm.get_diagnosis_basic())
            self.dm.update_diagnoses(diag_res.get("diagnosis_list"))
            
            # 2. Evaluate
            merged_diag = await self.evaluator.evaluate_diagnoses(
                self.dm.get_consolidated_diagnoses_basic(),
                diag_res.get("diagnosis_list"), 
                initial_history
            )
            self.dm.set_consolidated_diagnoses(merged_diag)
            
            # 3. Questions
            self.qm.add_questions_from_text(diag_res.get("follow_up_questions"))
            diag_stream = self.dm.get_consolidated_diagnoses()
            q_list = self.qm.get_recommend_question()
            
            # 4. Rank
            ranked_q = await self.ranker.rank_questions(initial_history, diag_stream, q_list)
            self.qm.update_ranking(ranked_q)

            # 5. Push Updates
            self.shared_state["ranked_questions"] = self.qm.get_recommend_question()
            await self.websocket.send_json({"type": "diagnosis", "data": diag_stream})
            await self.websocket.send_json({"type": "questions", "data": self.qm.get_questions()})
            
            logger.info("✅ Init Logic Complete")

        except Exception as e:
            logger.error(f"Init Error: {e}")
            await self.websocket.send_json({"type": "system", "message": "Init Error, proceeding..."})

        # --- START BACKGROUND MONITORING ---
        logic_thread = ClinicalLogicThread(
            self.tm, self.qm, self.dm, self.shared_state, 
            asyncio.get_running_loop(), self.websocket
        )
        logic_thread.start()

        # --- START VOICE LOOPS ---
        async with contextlib.AsyncExitStack() as stack:
            self.nurse.set_session(await stack.enter_async_context(self.nurse.get_connection_context()))
            self.patient.set_session(await stack.enter_async_context(self.patient.get_connection_context()))
            await self.websocket.send_json({"type": "system", "message": "Starting Assessment."})

            next_instruction = "Introduce yourself and ask for Name and DOB."
            patient_last_words = "Hello."
            interview_end = False
            last_qid = None
            
            while self.running:
                self.shared_state["cycle"] = self.cycle 

                # --- 1. NURSE ---
                nurse_input = f"Patient said: '{patient_last_words}'\n[SUPERVISOR: {next_instruction}]"
                nurse_text, _ = await self.nurse.speak_and_stream(nurse_input, self.websocket)
                
                if not nurse_text: nurse_text = "[The nurse waits]"
                self.tm.log("NURSE", nurse_text)

                await asyncio.sleep(0.5)
                await self.websocket.send_json({"type": "questions", "data": self.qm.get_questions()})

                # --- 2. PATIENT ---
                current_diagnosis_context = self.dm.get_consolidated_diagnoses_basic()
                patient_text, highlight_result = await self.patient.speak_and_stream(
                    nurse_text, 
                    self.websocket, 
                    highlighter=self.highlighter, 
                    diagnosis_context=current_diagnosis_context
                )
                
                if patient_text:
                    patient_last_words = patient_text
                else:
                    patient_text = "[The patient nods]"
                    patient_last_words = "(Silent)"

                if last_qid:
                    self.qm.update_answer(last_qid, patient_text)
                    await self.websocket.send_json({"type": "questions", "data": self.qm.get_questions()})

                self.tm.log("PATIENT", patient_text, highlight_data=highlight_result)
                await asyncio.sleep(0.5)
                await self.websocket.send_json({"type": "turn", "data": "finish cycle"})
                if interview_end: break

                # --- 3. ADVISOR ---
                try:
                    current_ranked = self.shared_state["ranked_questions"]
                    question, reasoning, status, qid = await self.advisor.get_advise(self.tm.get_history(), current_ranked)
                    
                    if qid: 
                        self.qm.update_status(qid, "asked")
                        last_qid = qid
                    
                    await self.websocket.send_json({"type": "system", "message": f"Logic: {reasoning}"})
                    
                    next_instruction = question
                    interview_end = status
                    self.cycle += 1

                except Exception as e:
                    logger.error(f"Main Loop Logic Error: {e}")
                    next_instruction = "Continue assessment."

                if self.websocket.client_state.name == "DISCONNECTED": break

            await self.websocket.send_json({"type": "turn", "data": "end"})

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


@app.post("/api/get-patient-file")
def get_patient_file(request: PatientFileRequest):
    """
    Retrieves a file from gs://clinic_sim/patient_profile/{pid}/{file_name}
    Handles JSON, Markdown, and PNG content types.
    """
    BUCKET_NAME = "clinic_sim"
    blob_path = f"patient_profile/{request.pid}/{request.file_name}"
    
    logger.info(f"📥 Fetching GCS: gs://{BUCKET_NAME}/{blob_path}")

    try:
        # Initialize GCS Client (Auth is automatic in Cloud Run)
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)

        if not blob.exists():
            logger.warning(f"File not found: {blob_path}")
            # Return 404 but as JSON so frontend handles it gracefully
            return JSONResponse(
                status_code=404, 
                content={"error": "File not found", "path": blob_path}
            )

        # Determine file type based on extension
        file_ext = request.file_name.lower().split('.')[-1]

        # --- HANDLE JSON ---
        if file_ext == 'json':
            content = blob.download_as_text()
            return JSONResponse(content=json.loads(content))

        # --- HANDLE MARKDOWN / TEXT ---
        elif file_ext in ['md', 'txt']:
            content = blob.download_as_text()
            return Response(content=content, media_type="text/markdown")

        # --- HANDLE IMAGES (PNG/JPG) ---
        elif file_ext in ['png', 'jpg', 'jpeg']:
            content = blob.download_as_bytes()
            media_type = "image/png" if file_ext == 'png' else "image/jpeg"
            return Response(content=content, media_type=media_type)

        # --- FALLBACK ---
        else:
            # Default to binary stream for unknown types
            content = blob.download_as_bytes()
            return Response(content=content, media_type="application/octet-stream")

    except Exception as e:
        logger.error(f"GCS API Error: {e}")
        return JSONResponse(
            status_code=500, 
            content={"error": str(e)}
        )
    

