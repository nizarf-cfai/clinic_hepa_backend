from typing import List, Dict, Any, Union

class DiagnosisManager:
    def __init__(self, high_threshold: int = 5, min_threshold: int = 3):
        """
        Args:
            high_threshold: Count required for 'High' probability.
            min_threshold: Count required for 'Medium'. Anything below this is 'Low'.
        """
        # Main recursive diagnosis pool
        self.diagnoses: List[Dict[str, Any]] = []
        
        # Consolidated diagnosis pool (Separate list for refined/merged results)
        self.consolidated_diagnoses: List[Dict[str, Any]] = []
        
        self.high_threshold = high_threshold
        self.min_threshold = min_threshold

    # ---------------------------------------------------------
    # CONSOLIDATED DIAGNOSIS FUNCTIONS
    # ---------------------------------------------------------
    def set_consolidated_diagnoses(self, raw_list: List[Dict[str, Any]]):
        """
        Accepts a list of diagnosis objects, calculates metrics, and stores them.
        """
        processed_list = []
        
        for item in raw_list:
            clean_item = {
                "did": item.get("did"),
                "diagnosis": item.get("diagnosis"),
                "indicators_point": item.get("indicators_point", [])
            }
            
            # Calculate count and probability
            self._recalculate_metrics(clean_item)
            processed_list.append(clean_item)

        # Sort by probability (High -> Low)
        priority_map = {"High": 3, "Medium": 2, "Low": 1}
        self.consolidated_diagnoses = sorted(
            processed_list, 
            key=lambda x: (priority_map.get(x["probability"], 0), x["indicators_count"]), 
            reverse=True
        )

    def get_consolidated_diagnoses(self) -> List[Dict[str, Any]]:
        """Returns the full consolidated list with metrics."""
        ranked_d = []
        for i, d in enumerate(self.consolidated_diagnoses):
            d['rank'] = i + 1
            points = len(d.get('indicators_point', [])) 

            # 1. HIGH: Must be Rank 1 (index 0) AND have > 8 points
            if (i == 0) and (points > 8):
                d['severity'] = "High"
                
            # 2. MODERATE: Points > 5 (This covers 6, 7, 8, AND >8 if Rank is not 1)
            elif points > 5:
                d['severity'] = "Moderate"
                
            # 3. LOW: Points 4, 5
            elif points > 3:
                d['severity'] = "Low"
                
            # 4. VERY LOW: Points <= 3
            else:
                d['severity'] = "Very Low"

            ranked_d.append(d)
        return ranked_d

    def get_consolidated_diagnoses_basic(self) -> List[Dict[str, Any]]:
        """
        Returns the consolidated list EXCLUDING 'indicators_count' and 'probability'.
        Keys returned: did, diagnosis, indicators_point.
        """
        simplified_list = []
        for item in self.consolidated_diagnoses:
            simplified_list.append({
                "did": item["did"],
                "diagnosis": item["diagnosis"],
                "indicators_point": item["indicators_point"]
            })
        return simplified_list

    # ---------------------------------------------------------
    # MAIN POOL FUNCTIONS
    # ---------------------------------------------------------
    def get_diagnosis_sum(self) -> List[Dict[str, Any]]:
        """Returns the WHOLE data object from main pool."""
        return self._get_sorted_list()

    def get_diagnosis_basic(self) -> List[Dict[str, Any]]:
        """Returns main pool excluding 'indicators_point' and 'probability'."""
        full_list = self._get_sorted_list()
        simplified_list = []
        for item in full_list:
            simplified_list.append({
                "did": item["did"],
                "diagnosis": item["diagnosis"],
                "indicators_point": item["indicators_point"] 
            })
        return simplified_list

    def get_diagnosis_normal(self) -> List[Dict[str, Any]]:
        """Returns main pool with diagnosis and points only."""
        full_list = self._get_sorted_list()
        simplified_list = []
        for item in full_list:
            simplified_list.append({
                "diagnosis": item["diagnosis"],
                "indicators_point": item["indicators_point"] 
            })
        return simplified_list

    # ---------------------------------------------------------
    # CORE LOGIC
    # ---------------------------------------------------------
    def update_diagnoses(self, new_data: List[Dict[str, Any]]):
        """
        Merges new diagnosis data into existing records (Recursive Pool).
        """
        for new_item in new_data:
            target_did = new_item.get("did")
            existing_item = self._find_by_did(target_did)

            if existing_item:
                current_points = set(existing_item.get("indicators_point", []))
                new_points = set(new_item.get("indicators_point", []))
                merged_points = list(current_points.union(new_points))
                existing_item["indicators_point"] = merged_points
                self._recalculate_metrics(existing_item)
            else:
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

        # 0–3  -> Low
        # 4–5  -> Medium
        # >=6  -> High
        if count <= 3:
            item["probability"] = "Low"
        elif 4 <= count <= 5:
            item["probability"] = "Medium"
        else:
            item["probability"] = "High"

    def _get_sorted_list(self) -> List[Dict]:
        """Helper to return list sorted by Importance."""
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

    dm = DiagnosisManager()

    # Input from Agent
    agent_output = [
        {
            "did": "D1", 
            "diagnosis": "Influenza A", 
            "indicators_point": ["Fever", "Cough", "Body Ache", "Chills"]
        }
    ]

    # Set Data
    dm.set_consolidated_diagnoses(agent_output)

    # Get Basic Consolidated (No probability/count)
    result = dm.get_consolidated_diagnoses_basic()
    
    print(json.dumps(result, indent=2))