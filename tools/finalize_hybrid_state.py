from pathlib import Path
import re


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected 1 occurrence, found {count}")
    return source.replace(old, new, 1)


state_path = Path("backend/app/demo/state.py")
state = state_path.read_text(encoding="utf-8")
state = replace_once(
    state,
    '            "source": "DOM / Playwright",\n            "status": "WAITING",\n',
    '            "source": "HYBRID / DOM + Canvas Vision",\n            "status": "WAITING",\n',
    "initial market reader source",
)
state_path.write_text(state, encoding="utf-8")

engine_path = Path("backend/app/demo/engine.py")
engine = engine_path.read_text(encoding="utf-8")
engine = replace_once(
    engine,
    '        """Wait for current DOM odds without losing the active match or score."""\n',
    '        """Wait for current next-goal odds via DOM with Canvas Vision fallback."""\n',
    "wait-for-odds docstring",
)
engine = replace_once(
    engine,
    '                f"Ждём DOM-рынок следующего гола №{next_goal_number}",\n',
    '                f"Ждём рынок следующего гола №{next_goal_number} (DOM / Canvas Vision)",\n',
    "wait-for-odds status message",
)
old_reader = '''                market_reader={
                    "source": "DOM / Playwright",
                    "status": "READING",
                    "attempt": attempt,
                    "next_goal_number": next_goal_number,
                },
                odds={
                    "selected": None,
                    "opponent": None,
                    "team1": None,
                    "team2": None,
                    "market": f"Следующий гол №{next_goal_number}",
                    "source": "DOM_PLAYWRIGHT",
                    "backend": None,
                    "confidence": None,
                    "status": "WAITING_FOR_MARKET",
                },
'''
new_reader = '''                market_reader={
                    "source": "HYBRID / DOM + Canvas Vision",
                    "status": "READING",
                    "attempt": attempt,
                    "next_goal_number": next_goal_number,
                },
                odds={
                    "selected": None,
                    "opponent": None,
                    "team1": None,
                    "team2": None,
                    "market": f"Следующий гол №{next_goal_number}",
                    "source": None,
                    "backend": None,
                    "confidence": None,
                    "status": "WAITING_FOR_MARKET",
                },
'''
engine = replace_once(engine, old_reader, new_reader, "hybrid waiting state")
compile(engine, str(engine_path), "exec")
engine_path.write_text(engine, encoding="utf-8")

readme_path = Path("README.md")
readme = readme_path.read_text(encoding="utf-8")
readme, count = re.subn(
    r"\n## Вилки настольного тенниса \(DEMO\).*?(?=\n## Canvas OCR)",
    "",
    readme,
    count=1,
    flags=re.S,
)
if count != 1:
    raise SystemExit(f"README tennis section: expected 1 occurrence, found {count}")
readme = readme.replace(
    "## Canvas OCR\n\nОсновной движок — singleton `RapidOCR` (ONNX Runtime), fallback —\n"
    "`pytesseract`. Tesseract ищется через `TESSERACT_CMD`, `PATH` и стандартные\n"
    "Windows-каталоги. Отсутствие Tesseract не мешает работе RapidOCR.\n",
    "## Следующий гол: DOM + Canvas Vision\n\n"
    "Рабочий reader сначала использует DOM, если исходы рынка доступны как обычные\n"
    "элементы. Когда коэффициенты отрисованы в Canvas, автоматически включается\n"
    "машинное зрение: OpenCV + `RapidOCR` (ONNX Runtime), с `pytesseract` как\n"
    "fallback. Перед использованием коэффициентов проверяются стабильность двух\n"
    "чтений и номер следующего гола относительно текущего счёта. В LIVE координаты\n"
    "OCR переводятся в CSS-координаты Canvas перед открытием нужного исхода.\n",
)
if "Вилки настольного тенниса" in readme:
    raise SystemExit("README tennis section was not fully removed")
readme_path.write_text(readme, encoding="utf-8")

test_path = Path("tests/test_runtime_wiring.py")
tests = test_path.read_text(encoding="utf-8")
marker = "    def test_engine_uses_hybrid_market_reader(self):\n"
if marker not in tests:
    raise SystemExit("runtime wiring test marker missing")
if "test_next_goal_wait_state_is_hybrid_not_dom_only" not in tests:
    extra = '''    def test_next_goal_wait_state_is_hybrid_not_dom_only(self):
        state_source = Path("backend/app/demo/state.py").read_text(encoding="utf-8")
        engine_source = Path("backend/app/demo/engine.py").read_text(encoding="utf-8")
        self.assertIn("HYBRID / DOM + Canvas Vision", state_source)
        self.assertIn("Ждём рынок следующего гола", engine_source)
        self.assertNotIn("Ждём DOM-рынок следующего гола", engine_source)

'''
    tests = tests.replace(marker, extra + marker, 1)
test_path.write_text(tests, encoding="utf-8")
