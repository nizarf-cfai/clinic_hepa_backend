# --- agents.py ---
import os
import json
import base64
import uuid
import asyncio
import logging
from google import genai
from google.genai import types
from fastapi import WebSocket

# Configure logging
logger = logging.getLogger("medforce-backend")

# --- Configuration ---
VOICE_MODEL = "gemini-live-2.5-flash-preview-native-audio-09-2025"
ADVISOR_MODEL = "gemini-2.5-flash" 
DIAGNOSER_MODEL = "gemini-2.5-flash-lite" 
RANKER_MODEL = "gemini-2.5-flash-lite" 

class BaseLogicAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))

class QuestionRankingAgent(BaseLogicAgent):
    def __init__(self, patient_info):
        super().__init__()
        self.response_schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"rank": { "type": "INTEGER" }, "qid": { "type": "STRING" }}, "required": ["rank", "qid"]}}
        self.patient_info = patient_info
        try:
            with open("patient_profile/q_ranker.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Rank by priority."

    async def rank_questions(self, conversation_history, current_diagnosis, q_list):
        prompt = f"Patient Profile:\n{self.patient_info}\n\nHistory:\n{json.dumps(conversation_history)}\n\nDiagnosis:\n{json.dumps(current_diagnosis)}\n\nQuestions:\n{json.dumps(q_list)}"
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
    def __init__(self, patient_info):
        super().__init__()
        self.response_schema = {"type": "OBJECT", "properties": {"diagnosis_list": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"diagnosis": { "type": "STRING" }, "did": { "type": "STRING" }, "indicators_point": { "type": "ARRAY", "items": { "type": "STRING" } }}, "required": ["diagnosis", "indicators_point", "did"]}}, "follow_up_questions": {"type": "ARRAY", "items": { "type": "STRING" }}}, "required": ["diagnosis_list", "follow_up_questions"]}
        self.patient_info = patient_info
        try:
            with open("patient_profile/diagnoser.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Diagnose patient."

    async def get_diagnosis_update(self, interview_data, current_diagnosis_hypothesis):
        prompt = f"Patient:\n{self.patient_info}\n\nTranscript:\n{json.dumps(interview_data)}\n\nState:\n{json.dumps(current_diagnosis_hypothesis)}"
        try:
            response = await self.client.aio.models.generate_content(
                model=DIAGNOSER_MODEL, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.2)
            )
            res = json.loads(response.text)
            return {"diagnosis_list": res.get("diagnosis_list", []), "follow_up_questions": res.get("follow_up_questions", [])}
        except: return {"diagnosis_list": current_diagnosis_hypothesis, "follow_up_questions": []}

class AdvisorAgent(BaseLogicAgent):
    def __init__(self, patient_info):
        super().__init__()
        self.response_schema = {"type": "OBJECT", "properties": {"question": { "type": "STRING" }, "qid": { "type": "STRING" }, "end_conversation": { "type": "BOOLEAN" }, "reasoning": { "type": "STRING" }}, "required": ["question", "end_conversation", "reasoning", "qid"]}
        self.patient_info = patient_info
        try:
            with open("patient_profile/advisor_agent.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Advise nurse."

    async def get_advise(self, conversation_history, q_list):
        prompt = f"Context:\n{self.patient_info}\n\nHistory:\n{json.dumps(conversation_history)}\n\nQuestions:\n{json.dumps(q_list)}"
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

class TextBridgeAgent:
    def __init__(self, name, system_instruction, voice_name):
        self.name = name
        self.system_instruction = system_instruction
        self.voice_name = voice_name
        self.client = genai.Client(
            vertexai=True, 
            project=os.getenv("PROJECT_ID"), 
            location=os.getenv("PROJECT_LOCATION", "us-central1")
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
                    await websocket.send_json({
                        "type": "audio",
                        "id": turn_id,
                        "speaker": self.name,
                        "data": b64_audio
                    })
                    await asyncio.sleep(0.005) 

                if response.server_content and response.server_content.output_transcription:
                    if text_chunk := response.server_content.output_transcription.text:
                        text_accumulator.append(text_chunk)
                        await websocket.send_json({
                            "type": "text_delta",
                            "id": turn_id,
                            "speaker": self.name,
                            "text": text_chunk,
                        })

                if response.server_content and response.server_content.turn_complete:
                    await websocket.send_json({
                        "type": "turn_complete",
                        "id": turn_id,
                        "speaker": self.name
                    })
                    
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