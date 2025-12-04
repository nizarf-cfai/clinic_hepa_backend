import asyncio
import os
import json
import base64
import contextlib
import logging
import datetime
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

# Configure logging for Cloud Run
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medforce-backend")

load_dotenv()

app = FastAPI()

# Allow CORS for frontend access
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
        "Patient Name and Date of Birth confirmed",
        "Chief Complaint identified",
        "Pain Level (severity) quantified",
        "Duration of symptoms established",
        "Current Medications listed",
        "Drug Allergies listed",
        "Past Surgeries listed"
    ]
except Exception as e:
    logger.error(f"Failed to load static files: {e}")
    # Fallbacks to prevent crash on boot, though app won't work correctly without them
    QUESTION_LIST = []
    PATIENT_PROFILE_TEXT = ""
    NURSE_PROMPT = "You are a nurse."
    PATIENT_PROMPT = "You are a patient."


# --- LOGIC AGENTS (Copied and adapted for Server) ---

class QuestionRankingAgent:
    def __init__(self):
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("PROJECT_ID"),
            location=os.getenv("PROJECT_LOCATION", "us-central1"),
        )
        self.response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "rank": { "type": "INTEGER" },
                    "qid": { "type": "STRING" }
                },
                "required": ["rank", "qid"]
            }
        }
        try:
            with open("patient_profile/q_ranker.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except FileNotFoundError:
            self.system_instruction = "Rank questions by priority."

    async def rank_questions(self, conversation_history, current_diagnosis, q_list):
        prompt = (
            f"Patient Profile:\n{PATIENT_PROFILE_TEXT}\n\n"
            f"Conversation History:\n{json.dumps(conversation_history, indent=2)}\n\n"
            f"Current Diagnosis Hypotheses:\n{json.dumps(current_diagnosis, indent=2)}\n\n"
            f"Candidate Questions:\n{json.dumps(q_list, indent=2)}\n\n"
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=RANKER_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self.response_schema,
                    system_instruction=self.system_instruction,
                    temperature=0.1 
                )
            )
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Ranking Error: {e}")
            return [{"rank": i+1, "qid": q["qid"]} for i, q in enumerate(q_list)]

class DiagnoseAgent:
    def __init__(self):
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("PROJECT_ID"),
            location=os.getenv("PROJECT_LOCATION", "us-central1"),
        )
        self.response_schema = {
            "type": "OBJECT",
            "properties": {
                "diagnosis_list": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "diagnosis": { "type": "STRING" },
                            "did": { "type": "STRING" },
                            "indicators_point": { 
                                "type": "ARRAY", 
                                "items": { "type": "STRING" } 
                            }
                        },
                        "required": ["diagnosis", "indicators_point", "did"]
                    }
                },
                "follow_up_questions": {
                    "type": "ARRAY",
                    "items": { "type": "STRING" }
                }
            },
            "required": ["diagnosis_list", "follow_up_questions"]
        }
        try:
            with open("patient_profile/diagnoser.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except FileNotFoundError:
            self.system_instruction = "You are a medical diagnoser."

    async def get_diagnosis_update(self, interview_data, current_diagnosis_hypothesis):
        prompt = (
            f"--- PATIENT INFO ---\n{PATIENT_PROFILE_TEXT}\n\n"
            f"--- INTERVIEW TRANSCRIPT ---\n{json.dumps(interview_data)}\n\n"
            f"--- CURRENT HYPOTHESIS STATE ---\n{json.dumps(current_diagnosis_hypothesis)}\n\n"
            f"Task: Update hypothesis."
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=DIAGNOSER_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self.response_schema,
                    system_instruction=self.system_instruction,
                    temperature=0.2
                )
            )
            result = json.loads(response.text)
            return {
                "diagnosis_list": result.get("diagnosis_list", []),
                "follow_up_questions": result.get("follow_up_questions", [])
            }
        except Exception as e:
            logger.error(f"Diagnoser Error: {e}")
            return {
                "diagnosis_list": current_diagnosis_hypothesis, 
                "follow_up_questions": [],
                "error": str(e)
            }

class AdvisorAgent:
    def __init__(self):
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("PROJECT_ID"),
            location=os.getenv("PROJECT_LOCATION", "us-central1"),
        )
        self.response_schema = {
            "type": "OBJECT",
            "properties": {
                "question": { "type": "STRING" },
                "qid": { "type": "STRING" },
                "end_conversation": { "type": "BOOLEAN" },
                "reasoning": { "type": "STRING" }
            },
            "required": ["question", "end_conversation", "reasoning", "qid"]
        }
        try:
            with open("patient_profile/advisor_agent.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except FileNotFoundError:
            self.system_instruction = "Advise the nurse on what to ask next."

    async def get_advise(self, conversation_history, q_list):
        prompt = (
            f"Patient Context:\n{PATIENT_PROFILE_TEXT}\n\n"
            f"Conversation History:\n{json.dumps(conversation_history)}\n\n"
            f"COMPLETION CHECKLIST:\n{json.dumps(COMPLETION_CHECKLIST)}\n\n"
            f"Available Questions:\n{json.dumps(q_list)}\n\n"
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=ADVISOR_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self.response_schema,
                    system_instruction=self.system_instruction,
                    temperature=0.2
                )
            )
            result = json.loads(response.text)
            if result.get("end_conversation"):
                return "Thank the patient and end the call.", result.get("reasoning"), True
            else:
                return result.get("question"), result.get("reasoning"), False
        except Exception as e:
            logger.error(f"Advisor Error: {e}")
            return "Continue.", "Error", False


# --- VOICE AGENTS ---

class TextBridgeAgent:
    def __init__(self, name, system_instruction, voice_name):
        self.name = name
        self.system_instruction = system_instruction
        self.voice_name = voice_name
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("PROJECT_ID"), 
            location=os.getenv("PROJECT_LOCATION", "us-central1"),
        )
        self.session = None

    def get_connection_context(self):
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"], 
            system_instruction=types.Content(parts=[types.Part(text=self.system_instruction)]),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name)
                )
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        return self.client.aio.live.connect(model=VOICE_MODEL, config=config)

    def set_session(self, session):
        self.session = session

    async def speak_and_stream(self, text_input, websocket: WebSocket):
        if not self.session: return None

        # Send Input
        await self.session.send(input=text_input, end_of_turn=True)

        text_accumulator = []
        
        try:
            async for response in self.session.receive():
                # 1. Stream Audio to Frontend
                if data := response.data:
                    b64_audio = base64.b64encode(data).decode('utf-8')
                    await websocket.send_json({
                        "type": "audio",
                        "speaker": self.name,
                        "data": b64_audio
                    })
                    # Small yield to keep stream smooth
                    await asyncio.sleep(0.01)

                # 2. Collect Text
                if response.server_content and response.server_content.output_transcription:
                    if text := response.server_content.output_transcription.text:
                        text_accumulator.append(text)

                # 3. End of Turn
                if response.server_content and response.server_content.turn_complete:
                    full_text = "".join(text_accumulator).strip()
                    
                    if full_text:
                        logger.info(f"📝 {self.name}: {full_text}")
                        # Send text transcript to UI
                        await websocket.send_json({
                            "type": "transcript",
                            "speaker": self.name,
                            "text": full_text
                        })
                        return full_text
                    
                    # Fallback for empty text but valid turn end
                    return "[...]"
                    
            return None
        except Exception as e:
            logger.error(f"Error in {self.name}: {e}")
            return None


# --- ORCHESTRATOR ---

class SimulationManager:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.nurse = TextBridgeAgent("NURSE", NURSE_PROMPT, "Aoede")
        self.patient = TextBridgeAgent("PATIENT", PATIENT_PROMPT, "Puck")
        
        # Logic Agents
        self.advisor = AdvisorAgent()
        self.diagnoser = DiagnoseAgent()
        self.q_rank = QuestionRankingAgent()
        self.qm = question_manager.QuestionPoolManager(QUESTION_LIST)
        self.diagnosis_manager = diagnosis_manager.DiagnosisManager()
        
        # Conversation History (List of dicts: {speaker, text, timestamp})
        self.history = []
        self.running = False

    def add_to_history(self, speaker, text):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.history.append({
            "timestamp": timestamp,
            "speaker": speaker,
            "text": text
        })

    async def run(self):
        self.running = True
        await self.websocket.send_json({"type": "system", "message": "Initializing Agents..."})

        async with contextlib.AsyncExitStack() as stack:
            # Connect Voice Agents
            nurse_ctx = self.nurse.get_connection_context()
            self.nurse.set_session(await stack.enter_async_context(nurse_ctx))

            pat_ctx = self.patient.get_connection_context()
            self.patient.set_session(await stack.enter_async_context(pat_ctx))

            await self.websocket.send_json({"type": "system", "message": "Connected. Starting Assessment."})

            # Logic Variables
            next_instruction = "Introduce yourself and ask for Name and DOB."
            patient_last_words = "Hello."
            interview_end = False

            while self.running:
                if self.websocket.client_state.name == "DISCONNECTED": break

                # --- 1. NURSE TURN ---
                nurse_input = f"Patient said: '{patient_last_words}'\n[SUPERVISOR INSTRUCTION: {next_instruction}]"
                nurse_text = await self.nurse.speak_and_stream(nurse_input, self.websocket)
                
                if not nurse_text:
                    nurse_text = "[The nurse waits]"
                
                self.add_to_history("NURSE", nurse_text)

                if "doctor" in next_instruction.lower() and "bye" in nurse_text.lower():
                    await self.websocket.send_json({"type": "system", "message": "Conversation Ended by Protocol."})
                    break

                await asyncio.sleep(0.5)

                # --- 2. PATIENT TURN ---
                patient_text = await self.patient.speak_and_stream(nurse_text, self.websocket)
                
                if not patient_text:
                    patient_text = "[The patient nods]"
                
                patient_last_words = patient_text
                self.add_to_history("PATIENT", patient_text)
                
                await asyncio.sleep(0.5)

                if interview_end:
                    break

                # --- 3. ANALYSIS PHASE (Parallel Logic) ---
                # We do this logic *after* the patient speaks to decide what the nurse asks next
                try:
                    await self.websocket.send_json({"type": "system", "message": "Analyzing Response..."})
                    
                    # A. Diagnosis Update
                    diagnose_result = await self.diagnoser.get_diagnosis_update(
                        self.history, 
                        self.diagnosis_manager.get_diagnosis_basic()
                    )
                    self.diagnosis_manager.update_diagnoses(diagnose_result.get("diagnosis_list"))
                    
                    diagnosis_current = self.diagnosis_manager.get_diagnosis_basic()
                    diagnosis_stream = self.diagnosis_manager.get_diagnosis_sum()

                    # >> STREAM DIAGNOSIS TO FRONTEND <<
                    await self.websocket.send_json({
                        "type": "diagnosis",
                        "data": diagnosis_stream
                    })

                    # B. Question Ranking
                    self.qm.add_questions_from_text(diagnose_result.get("follow_up_questions"))
                    q_list = self.qm.get_recommend_question()
                    
                    update_q = await self.q_rank.rank_questions(self.history, diagnosis_current, q_list)
                    self.qm.update_ranking(update_q)
                    
                    q_list = self.qm.get_recommend_question()
                    q_list_stream = self.qm.get_questions() # Full list/stream

                    # >> STREAM QUESTIONS TO FRONTEND <<
                    await self.websocket.send_json({
                        "type": "questions",
                        "data": q_list_stream
                    })

                    # C. Advisor Decision
                    question, reasoning, status = await self.advisor.get_advise(self.history, q_list)
                    
                    # >> STREAM ADVISOR THOUGHTS (Optional, reusing system type or creating new)
                    await self.websocket.send_json({
                        "type": "system",
                        "message": f"Logic: {reasoning}"
                    })

                    next_instruction = question
                    interview_end = status

                except Exception as e:
                    logger.error(f"Logic Error: {e}")
                    await self.websocket.send_json({"type": "system", "message": "Analysis Error, continuing..."})
                    next_instruction = "Continue assessment."


@app.websocket("/ws/simulation")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    manager = SimulationManager(websocket)
    try:
        data = await websocket.receive_text()
        if data == "start":
            await manager.run()
    except WebSocketDisconnect:
        logger.info("Client disconnected")
        manager.running = False
    except Exception as e:
        logger.error(f"WebSocket Error: {e}")