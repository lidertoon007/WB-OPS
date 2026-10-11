import json
from pathlib import Path


class QuestionKnowledge:
    """Read-only historical Q&A reference.

    Exact question matches may reuse a prior answer. New questions use the
    rule-based templates so stale product facts are not copied blindly.
    """

    def __init__(self, path: str = "app/knowledge/questions.json"):
        self.records: list[dict[str, object]] = []
        source = Path(path)
        if source.exists():
            self.records = json.loads(source.read_text(encoding="utf-8")).get("records", [])

    def exact_answer(self, question: str, product_id: int | None) -> str | None:
        normalized = " ".join(question.lower().split())
        for record in self.records:
            if record.get("nm_id") == product_id and " ".join(str(record.get("question", "")).lower().split()) == normalized:
                answer = str(record.get("answer", "")).strip()
                if answer:
                    return answer
        return None
