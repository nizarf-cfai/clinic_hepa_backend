from typing import List, Dict, Any, Union

class DiagnosisManager:
    def __init__(self, high_threshold: int = 5, min_threshold: int = 3):
        """
        Args:
            high_threshold: Count required for 'High' probability.
            min_threshold: Count required for 'Medium'. Anything below this is 'Low'.
        """
        self.diagnoses: List[Dict[str, Any]] = []
        self.high_threshold = high_threshold
        self.min_threshold = min_threshold

    # ---------------------------------------------------------
    # DATA RETRIEVAL FUNCTIONS
    # ---------------------------------------------------------
    def get_diagnosis_sum(self) -> List[Dict[str, Any]]:
        """
        Returns the WHOLE data object (all keys), sorted by probability.
        """
        return self._get_sorted_list()

    def get_diagnosis_basic(self) -> List[Dict[str, Any]]:
        """
        Returns a simplified list EXCLUDING 'indicators_point' and 'probability'.
        Useful for UI dropdowns or summaries where details aren't needed.
        """
        full_list = self._get_sorted_list()
        simplified_list = []
        
        for item in full_list:
            simplified_list.append({
                "did": item["did"],
                "diagnosis": item["diagnosis"],
                "indicators_point": item["indicators_point"] 
                # Note: indicators_point and probability are EXCLUDED here
            })
            
        return simplified_list

    # ---------------------------------------------------------
    # CORE LOGIC
    # ---------------------------------------------------------
    def update_diagnoses(self, new_data: List[Dict[str, Any]]):
        """
        Merges new diagnosis data into existing records.
        - Adds new indicators to the existing list (UNION).
        - Does NOT overwrite existing indicators.
        - Removes duplicates automatically.
        """
        for new_item in new_data:
            target_did = new_item.get("did")
            existing_item = self._find_by_did(target_did)

            if existing_item:
                # 1. Get existing points (default to empty list if missing)
                current_points = set(existing_item.get("indicators_point", []))
                
                # 2. Get new points
                new_points = set(new_item.get("indicators_point", []))
                
                # 3. Merge: Union keeps all unique items from both sets
                merged_points = list(current_points.union(new_points))
                
                # 4. Update the object
                existing_item["indicators_point"] = merged_points
                self._recalculate_metrics(existing_item)
                
            else:
                # Add new diagnosis entirely
                clean_item = {
                    "diagnosis": new_item["diagnosis"],
                    "did": new_item["did"],
                    "indicators_point": new_item.get("indicators_point", [])
                }
                self._recalculate_metrics(clean_item)
                self.diagnoses.append(clean_item)

    # ---------------------------------------------------------
    # HELPERS
    # ---------------------------------------------------------
    def _find_by_did(self, did: str) -> Union[Dict, None]:
        """Find a diagnosis object by its ID."""
        for item in self.diagnoses:
            if item["did"] == did:
                return item
        return None

    def _recalculate_metrics(self, item: Dict):
        """
        Updates count and applies strict threshold logic.
        """
        count = len(item["indicators_point"])
        item["indicators_count"] = count

        # New logic:
        # 0–3  -> Low
        # 4–5  -> Medium
        # >=6  -> High
        if count <= 3:
            item["probability"] = "Low"
        elif 4 <= count <= 5:
            item["probability"] = "Medium"
        else:  # count >= 6
            item["probability"] = "High"

    def _get_sorted_list(self) -> List[Dict]:
        """Helper to return list sorted by Importance (High > Med > Low)."""
        priority_map = {"High": 3, "Medium": 2, "Low": 1}
        return sorted(
            self.diagnoses, 
            key=lambda x: (priority_map.get(x["probability"], 0), x["indicators_count"]), 
            reverse=True
        )

# ==========================================
# EXAMPLE USAGE
# ==========================================
if __name__ == "__main__":
    import json

    # 1. Initialize
    dm = DiagnosisManager(high_threshold=5, min_threshold=3)

    # 2. First Update (Initial Analysis)
    input_batch_1 = [
        {
            "diagnosis": "Angina",
            "did": "A001",
            "indicators_point": ["Chest Pain", "Shortness of Breath"]
        }
    ]
    dm.update_diagnoses(input_batch_1)
    
    print("--- State 1 (Initial) ---")
    print(json.dumps(dm.get_diagnosis_sum(), indent=2))

    # 3. Second Update (New info found)
    # NOTICE: "Chest Pain" is repeated. "Arm Numbness" is new.
    input_batch_2 = [
        {
            "diagnosis": "Angina",
            "did": "A001",
            "indicators_point": ["Chest Pain", "Arm Numbness"] 
        }
    ]
    
    dm.update_diagnoses(input_batch_2)

    print("\n--- State 2 (Merged - Check indicators_point) ---")
    # Expected: 3 items ["Chest Pain", "Shortness of Breath", "Arm Numbness"]
    print(json.dumps(dm.get_diagnosis_sum(), indent=2))