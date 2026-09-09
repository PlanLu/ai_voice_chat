import json
from pathlib import Path


class VoiceprintRegistry:
    """在本地保存姓名与云端声纹 ID 的长期对应关系。"""

    def __init__(self, path="voiceprints.json"):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, name, voiceprint_id):
        people = self.load()
        people[name] = voiceprint_id
        self.path.write_text(
            json.dumps(people, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def voiceprint_ids(self):
        return list(self.load().values())

    def name_for(self, voiceprint_id):
        for name, registered_id in self.load().items():
            if registered_id == voiceprint_id:
                return name
        return None
