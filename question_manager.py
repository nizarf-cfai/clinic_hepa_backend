import uuid
from typing import List, Dict, Optional, Any

class QuestionPoolManager:
    def __init__(self, initial_questions: List[Dict[str, Any]], 
                 default_max_score: int = 10, 
                 decay_step: int = 1, 
                 min_score: int = 1):
        self.questions = initial_questions
        self.default_max_score = default_max_score
        self.decay_step = decay_step
        self.min_score = min_score

    # ---------------------------------------------------------
    # FUNCTION 1: Add Questions (With "Resurrection" Logic)
    # ---------------------------------------------------------
    def add_questions_from_text(self, text_list: List[str]) -> List[Dict]:
        """
        Adds questions. If a question already exists but was 'deleted', 
        it 'resurrects' it (sets status back to None).
        """
        new_objects = []
        
        for text in text_list:
            clean_text = text.strip()
            if not clean_text:
                continue
                
            # Check if content exists ANYWHERE in the pool (Active, Asked, or Deleted)
            # We search efficiently by lower-case match
            existing_q = next((q for q in self.questions if q["content"].lower().strip() == clean_text.lower()), None)

            if existing_q:
                # SCENARIO A: Question exists but was marked "deleted" by the ranker previously.
                # ACTION: Resurrect it! The agent thinks it's relevant again.
                if existing_q.get("status") == "deleted":
                    existing_q["status"] = None  # Make active
                    existing_q["rank"] = 999     # Reset rank
                    existing_q["score"] = 0      # Reset score
                    new_objects.append(existing_q)
                
                # SCENARIO B: Question exists and is "asked" or already active.
                # ACTION: Do nothing. Don't duplicate.
                continue

            # SCENARIO C: Truly new question.
            # ACTION: Create it.
            new_qid = str(uuid.uuid4())[:8] 
            new_q_obj = {
                "role": "nurse",
                "content": clean_text,
                "qid": new_qid,
                "score": 0,    
                "rank": 999,   
                "status": None 
            }
            self.questions.append(new_q_obj)
            new_objects.append(new_q_obj)

        return new_objects

    # ---------------------------------------------------------
    # FUNCTION 2: Update Ranking (With "Delete" Logic)
    # ---------------------------------------------------------
    def update_ranking(self, new_ranking_list: List[Dict]):
        """
        Updates Rank/Score. 
        - If an active question is NOT in the new list, mark it as "deleted".
        - "deleted" just hides it from recommendations; it stays in get_questions().
        """
        # 1. Map currently ACTIVE questions
        active_map = {q["qid"]: q for q in self.questions if q.get("status") is None}
        
        # 2. Get QIDs from the new input
        ranked_qids = set(item["qid"] for item in new_ranking_list)

        # 3. Calculate max score for continuity
        current_scores = [q["score"] for q in active_map.values() if "score" in q]
        max_score = max(current_scores) if current_scores else self.default_max_score

        # 4. Sort new input
        sorted_new_input = sorted(new_ranking_list, key=lambda x: x["rank"])

        # 5. Loop: Update Scores for kept questions
        for index, item in enumerate(sorted_new_input):
            qid = item["qid"]
            if qid in active_map:
                new_score = max(self.min_score, max_score - (index * self.decay_step))
                active_map[qid]["rank"] = item["rank"]
                active_map[qid]["score"] = new_score

        # 6. Loop: Mark unmentioned questions as 'deleted'
        for qid, q_obj in active_map.items():
            if qid not in ranked_qids:
                q_obj["status"] = "deleted"
                q_obj["rank"] = 999 # Push to bottom logically

    # ---------------------------------------------------------
    # HELPER FUNCTIONS
    # ---------------------------------------------------------
    def get_recommend_question(self) -> List[Dict]:
        """Returns active questions only (Clean Output)."""
        active_questions = [q for q in self.questions if q.get("status") is None]
        sorted_qs = sorted(active_questions, key=lambda x: x.get("rank", 999))

        cleaned_list = []
        for q in sorted_qs:
            cleaned_list.append({
                "role": q.get("role", "nurse"),
                "content": q["content"],
                "qid": q["qid"]
            })
        return cleaned_list

    def get_questions(self) -> List[Dict]:
        """
        Returns the FULL history (Active, Asked, and Deleted).
        Nothing is ever removed from this list.
        """
        return self.questions

    def update_status(self, qid: str, new_status: str) -> bool:
        for q in self.questions:
            if q["qid"] == qid:
                q["status"] = new_status
                return True
        return False