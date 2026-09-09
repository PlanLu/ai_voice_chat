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
        self._write(people)

    def _write(self, people):
        self.path.write_text(
            json.dumps(people, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def remove_by_id(self, voiceprint_id):
        people = self.load()
        matched_name = next(
            (name for name, registered_id in people.items()
             if registered_id == voiceprint_id),
            None,
        )
        if matched_name is None:
            return None
        del people[matched_name]
        self._write(people)
        return matched_name

    def voiceprint_ids(self):
        return list(self.load().values())

    def name_for(self, voiceprint_id):
        for name, registered_id in self.load().items():
            if registered_id == voiceprint_id:
                return name
        return None
