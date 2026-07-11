"""
JARVIS AI OS — Reasoning Engine
================================
cognition/reasoning/reasoning_engine.py

Position in cognition pipeline:
    INPUT → [ReasoningEngine] → DecisionEngine → WorkflowPlanner
                                                        → Execution
                                    ReflectionEngine ←

Responsibilities:
    - Parse raw input into a structured ReasoningRequest
    - Decompose the problem into a chain of logical reasoning steps
    - Score each step for confidence and relevance
    - Produce a structured ReasoningResult for the DecisionEngine to consume
    - Emit reasoning events on the EventBus for observability

Architecture rules:
    - No kernel internals imported directly
    - All dependencies injected via inject()
    - All inter-module communication through EventBus only
    - Fully async; no blocking I/O on the event loop
    - Self-contained: works with or without model_router (falls back to
      rule-based decomposition when LLM is unavailable)
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ReasoningStrategy(str, Enum):
    """Strategy chosen by the engine to decompose a problem."""

    CHAIN_OF_THOUGHT = "chain_of_thought"  # linear logical steps
    DECOMPOSITION = "decomposition"  # break into independent sub-problems
    ANALOGY = "analogy"  # map to a known solved pattern
    ELIMINATION = "elimination"  # rule out impossible branches
    HYPOTHESIS = "hypothesis"  # generate + test hypotheses


class StepKind(str, Enum):
    """Semantic role of a single reasoning step."""

    OBSERVATION = "observation"  # what we know for certain
    INFERENCE = "inference"  # derived from observations
    ASSUMPTION = "assumption"  # plausible but unverified
    CONSTRAINT = "constraint"  # limits the solution space
    CONCLUSION = "conclusion"  # final answer / recommendation


class ComplexityBand(str, Enum):
    """Estimated problem complexity — determines decomposition depth."""

    TRIVIAL = "trivial"  # single-step answer
    SIMPLE = "simple"  # 2-3 steps
    MODERATE = "moderate"  # 4-7 steps, possible branching
    COMPLEX = "complex"  # 8+ steps, requires sub-problems
    UNKNOWN = "unknown"  # cannot estimate without LLM


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ReasoningStep:
    """
    A single logical step in a reasoning chain.

    Each step is self-contained: it carries the statement, the kind of
    reasoning it represents, a confidence score, and which prior steps
    it depends on.
    """

    step_id: str
    index: int
    kind: StepKind
    statement: str
    confidence: float  # 0.0 – 1.0
    depends_on: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "index": self.index,
            "kind": self.kind.value,
            "statement": self.statement,
            "confidence": round(self.confidence, 3),
            "depends_on": self.depends_on,
            "evidence": self.evidence,
            "alternatives": self.alternatives,
            "metadata": self.metadata,
        }


@dataclass
class ReasoningRequest:
    """
    Input to the ReasoningEngine — produced from raw user/system input.

    Callers may pre-populate context_facts and constraints to guide
    the reasoning chain without requiring LLM inference.
    """

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    raw_input: str = ""
    intent: str = ""  # extracted verb-phrase intent
    domain: str = "general"  # coding / research / system / general
    context_facts: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    priority: int = 2  # 1=critical … 5=low
    session_id: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class ReasoningResult:
    """
    Output produced by the ReasoningEngine — consumed by DecisionEngine.

    The `chain` field contains the full ordered list of reasoning steps.
    The `conclusion` field is a distilled final statement ready for
    the DecisionEngine to act on.
    The `confidence` field is the geometric mean of all step confidences,
    representing overall chain reliability.
    """

    result_id: str
    request_id: str
    strategy: ReasoningStrategy
    complexity: ComplexityBand
    chain: list[ReasoningStep]
    conclusion: str
    confidence: float  # aggregate chain confidence
    sub_problems: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "request_id": self.request_id,
            "strategy": self.strategy.value,
            "complexity": self.complexity.value,
            "chain": [s.to_dict() for s in self.chain],
            "conclusion": self.conclusion,
            "confidence": round(self.confidence, 3),
            "sub_problems": self.sub_problems,
            "flags": self.flags,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "created_at": self.created_at,
        }

    @property
    def is_reliable(self) -> bool:
        """True when the chain confidence meets the minimum threshold."""
        return self.confidence >= ReasoningEngine.MIN_RELIABLE_CONFIDENCE


# ---------------------------------------------------------------------------
# Intent + domain extraction (rule-based, no LLM required)
# ---------------------------------------------------------------------------


class _InputParser:
    """
    Lightweight static parser that extracts intent, domain, and complexity
    from raw text without an LLM.  Used as the fast-path and fallback.
    """

    _DOMAIN_SIGNALS: dict[str, list[str]] = {
        "coding": [
            "code",
            "bug",
            "error",
            "function",
            "class",
            "script",
            "python",
            "import",
            "debug",
            "test",
            "implement",
            "refactor",
            "compile",
            "syntax",
            "module",
            "api",
            "library",
        ],
        "research": [
            "research",
            "find",
            "search",
            "what is",
            "explain",
            "define",
            "summarise",
            "summarize",
            "article",
            "paper",
            "source",
            "wiki",
            "information",
            "learn",
            "read",
        ],
        "system": [
            "file",
            "folder",
            "directory",
            "disk",
            "cpu",
            "memory",
            "ram",
            "process",
            "service",
            "install",
            "uninstall",
            "update",
            "network",
            "port",
            "kill",
            "start",
            "stop",
            "restart",
            "log",
            "monitor",
        ],
        "planning": [
            "plan",
            "schedule",
            "task",
            "project",
            "deadline",
            "milestone",
            "remind",
            "calendar",
            "organise",
            "organize",
            "workflow",
            "steps",
            "goal",
            "objective",
        ],
        "communication": [
            "email",
            "message",
            "send",
            "reply",
            "slack",
            "notify",
            "notification",
            "tell",
            "inform",
            "contact",
            "call",
        ],
    }

    _INTENT_VERBS: list[str] = [
        "create",
        "build",
        "write",
        "make",
        "generate",
        "produce",
        "find",
        "search",
        "look up",
        "research",
        "retrieve",
        "fix",
        "debug",
        "repair",
        "solve",
        "resolve",
        "explain",
        "describe",
        "summarise",
        "summarize",
        "analyse",
        "analyze",
        "run",
        "execute",
        "start",
        "launch",
        "open",
        "delete",
        "remove",
        "clean",
        "clear",
        "update",
        "modify",
        "change",
        "edit",
        "refactor",
        "send",
        "notify",
        "inform",
        "tell",
        "plan",
        "schedule",
        "organise",
        "organize",
        "monitor",
        "watch",
        "track",
        "check",
    ]

    _COMPLEXITY_INDICATORS: dict[ComplexityBand, list[str]] = {
        ComplexityBand.TRIVIAL: ["what time", "open", "close", "show", "tell me"],
        ComplexityBand.SIMPLE: ["find", "search", "look up", "read"],
        ComplexityBand.MODERATE: [
            "create",
            "build",
            "write",
            "fix",
            "debug",
            "explain",
        ],
        ComplexityBand.COMPLEX: [
            "refactor",
            "architect",
            "design",
            "migrate",
            "analyse",
            "analyze",
            "plan",
            "research",
        ],
    }

    @classmethod
    def parse(cls, raw: str) -> tuple[str, str, ComplexityBand]:
        """Return (intent, domain, complexity)."""
        lowered = raw.lower().strip()

        intent = cls._extract_intent(lowered)
        domain = cls._extract_domain(lowered)
        complexity = cls._estimate_complexity(lowered)

        return intent, domain, complexity

    @classmethod
    def _extract_intent(cls, text: str) -> str:
        for verb in cls._INTENT_VERBS:
            if verb in text:
                # Grab verb + up to 6 following words as intent phrase
                pattern = rf"{re.escape(verb)}(?:\s+\w+){{0,6}}"
                m = re.search(pattern, text)
                if m:
                    return m.group(0).strip()
        # Fallback: first 8 words
        words = text.split()
        return " ".join(words[:8])

    @classmethod
    def _extract_domain(cls, text: str) -> str:
        scores: dict[str, int] = {d: 0 for d in cls._DOMAIN_SIGNALS}
        for domain, signals in cls._DOMAIN_SIGNALS.items():
            for signal in signals:
                if signal in text:
                    scores[domain] += 1
        best = max(scores, key=scores.__getitem__)
        return best if scores[best] > 0 else "general"

    @classmethod
    def _estimate_complexity(cls, text: str) -> ComplexityBand:
        for band in (
            ComplexityBand.COMPLEX,
            ComplexityBand.MODERATE,
            ComplexityBand.SIMPLE,
            ComplexityBand.TRIVIAL,
        ):
            for indicator in cls._COMPLEXITY_INDICATORS[band]:
                if indicator in text:
                    return band
        # Length heuristic: longer inputs suggest more complex requests
        word_count = len(text.split())
        if word_count <= 5:
            return ComplexityBand.TRIVIAL
        if word_count <= 15:
            return ComplexityBand.SIMPLE
        if word_count <= 40:
            return ComplexityBand.MODERATE
        return ComplexityBand.COMPLEX


# ---------------------------------------------------------------------------
# Rule-based chain builder (LLM-free fallback)
# ---------------------------------------------------------------------------


class _RuleBasedChainBuilder:
    """
    Constructs a deterministic reasoning chain from a ReasoningRequest
    without calling any LLM.  Used when model_router is unavailable or
    when complexity is TRIVIAL/SIMPLE.
    """

    def build(
        self,
        request: ReasoningRequest,
        strategy: ReasoningStrategy,
        complexity: ComplexityBand,
    ) -> list[ReasoningStep]:
        steps: list[ReasoningStep] = []

        # Step 1 — Observation: anchor what we know
        obs = self._make_step(
            index=0,
            kind=StepKind.OBSERVATION,
            statement=(
                f"The request is to {request.intent or request.raw_input}. "
                f"Domain identified as '{request.domain}'."
            ),
            confidence=0.95,
        )
        steps.append(obs)

        # Step 2 — Constraints: surface known limits
        if request.constraints:
            for i, constraint in enumerate(request.constraints[:3]):
                steps.append(
                    self._make_step(
                        index=len(steps),
                        kind=StepKind.CONSTRAINT,
                        statement=constraint,
                        confidence=0.9,
                        depends_on=[obs.step_id],
                    )
                )
        else:
            steps.append(
                self._make_step(
                    index=len(steps),
                    kind=StepKind.CONSTRAINT,
                    statement="No explicit constraints provided; operating under default system policies.",
                    confidence=0.85,
                    depends_on=[obs.step_id],
                )
            )

        # Step 3 — Context injection
        if request.context_facts:
            for fact in request.context_facts[:4]:
                steps.append(
                    self._make_step(
                        index=len(steps),
                        kind=StepKind.OBSERVATION,
                        statement=f"Known context: {fact}",
                        confidence=0.9,
                        depends_on=[obs.step_id],
                    )
                )

        # Step 4 — Domain-specific inferences
        inferences = self._domain_inferences(request.domain, request.intent, complexity)
        prior_ids = [s.step_id for s in steps]
        for inf in inferences:
            steps.append(
                self._make_step(
                    index=len(steps),
                    kind=StepKind.INFERENCE,
                    statement=inf["statement"],
                    confidence=inf["confidence"],
                    depends_on=prior_ids,
                )
            )

        # Step 5 — Strategy-specific assumption
        if strategy == ReasoningStrategy.HYPOTHESIS:
            steps.append(
                self._make_step(
                    index=len(steps),
                    kind=StepKind.ASSUMPTION,
                    statement=(
                        "Assuming the most direct interpretation of the request "
                        "is correct; alternatives will be surfaced if this fails."
                    ),
                    confidence=0.75,
                    depends_on=prior_ids,
                    alternatives=[
                        "The request may require clarification from the user.",
                        "A sub-problem may need to be resolved first.",
                    ],
                )
            )

        # Final step — Conclusion
        conclusion_statement = self._derive_conclusion(request, steps)
        steps.append(
            self._make_step(
                index=len(steps),
                kind=StepKind.CONCLUSION,
                statement=conclusion_statement,
                confidence=self._aggregate_confidence(steps),
                depends_on=[s.step_id for s in steps],
            )
        )

        return steps

    # ------------------------------------------------------------------

    def _make_step(
        self,
        index: int,
        kind: StepKind,
        statement: str,
        confidence: float,
        depends_on: list[str] | None = None,
        alternatives: list[str] | None = None,
        evidence: list[str] | None = None,
    ) -> ReasoningStep:
        return ReasoningStep(
            step_id=uuid.uuid4().hex[:8],
            index=index,
            kind=kind,
            statement=statement,
            confidence=confidence,
            depends_on=depends_on or [],
            alternatives=alternatives or [],
            evidence=evidence or [],
        )

    def _domain_inferences(
        self,
        domain: str,
        intent: str,
        complexity: ComplexityBand,
    ) -> list[dict[str, Any]]:
        """Return domain-tuned inference statements."""
        base: list[dict[str, Any]] = []

        if domain == "coding":
            base = [
                {
                    "statement": "Code changes should be minimal, targeted, and tested.",
                    "confidence": 0.88,
                },
                {
                    "statement": (
                        "Existing patterns in the codebase should be "
                        "followed unless explicitly overriding them."
                    ),
                    "confidence": 0.82,
                },
            ]
        elif domain == "research":
            base = [
                {
                    "statement": (
                        "Multiple sources should be cross-referenced "
                        "before treating any claim as factual."
                    ),
                    "confidence": 0.85,
                },
                {
                    "statement": "Recent sources are preferred over older ones for fast-moving topics.",
                    "confidence": 0.80,
                },
            ]
        elif domain == "system":
            base = [
                {
                    "statement": "System operations must be non-destructive unless explicitly confirmed.",
                    "confidence": 0.95,
                },
                {
                    "statement": "Resource consumption should be minimised during execution.",
                    "confidence": 0.87,
                },
            ]
        elif domain == "planning":
            base = [
                {
                    "statement": "Tasks should be decomposed into the smallest independently-executable units.",
                    "confidence": 0.88,
                },
                {
                    "statement": "Dependencies between sub-tasks must be identified before scheduling.",
                    "confidence": 0.90,
                },
            ]
        else:
            base = [
                {
                    "statement": (
                        "The most direct path to the stated goal "
                        "should be preferred over elaborate alternatives."
                    ),
                    "confidence": 0.80,
                },
            ]

        if complexity in (ComplexityBand.COMPLEX, ComplexityBand.MODERATE):
            base.append(
                {
                    "statement": (
                        "Given the complexity of this request, partial results "
                        "should be surfaced incrementally rather than waiting "
                        "for full completion."
                    ),
                    "confidence": 0.78,
                }
            )

        return base

    def _derive_conclusion(
        self,
        request: ReasoningRequest,
        steps: list[ReasoningStep],
    ) -> str:
        inference_statements = [
            s.statement for s in steps if s.kind == StepKind.INFERENCE
        ]
        summary = (
            f"To fulfil '{request.intent or request.raw_input}' in the "
            f"'{request.domain}' domain: "
        )
        if inference_statements:
            summary += inference_statements[0]
            summary += " Execution should proceed through the WorkflowPlanner."
        else:
            summary += (
                "Proceed with direct execution via the DecisionEngine "
                "without further decomposition required."
            )
        return summary

    @staticmethod
    def _aggregate_confidence(steps: list[ReasoningStep]) -> float:
        """Geometric mean of all non-conclusion step confidences."""
        scores = [s.confidence for s in steps if s.kind != StepKind.CONCLUSION]
        if not scores:
            return 0.5
        product = 1.0
        for s in scores:
            product *= max(s, 1e-9)
        return round(product ** (1.0 / len(scores)), 3)


# ---------------------------------------------------------------------------
# LLM-assisted chain builder (used when model_router is available and
# complexity is MODERATE or COMPLEX)
# ---------------------------------------------------------------------------


class _LLMChainBuilder:
    """
    Calls the injected model_router to produce a richer reasoning chain
    for moderate-to-complex problems.  Falls back to rule-based output
    on any exception so the pipeline never stalls.
    """

    _SYSTEM_PROMPT = """You are the Reasoning Engine of JARVIS AI OS.
Your job is to decompose a problem into a structured JSON reasoning chain.

Output ONLY valid JSON in this exact schema:
{
  "strategy": "<chain_of_thought|decomposition|analogy|elimination|hypothesis>",
  "steps": [
    {
      "kind": "<observation|inference|assumption|constraint|conclusion>",
      "statement": "<concise logical statement>",
      "confidence": <float 0.0-1.0>,
      "depends_on_indexes": [<int>, ...],
      "evidence": ["<fact>", ...],
      "alternatives": ["<alternative>", ...]
    }
  ],
  "sub_problems": ["<sub-problem string>", ...],
  "flags": ["<flag>", ...]
}

Rules:
- The last step must be kind="conclusion"
- confidence reflects how certain you are about this step
- Keep each statement to one clear sentence
- sub_problems lists anything that needs separate resolution
- flags lists concerns like "ambiguous_intent", "missing_context", "high_risk"
- Output nothing except the JSON object
"""

    def __init__(self, model_router: Any) -> None:
        self._model = model_router

    async def build(
        self,
        request: ReasoningRequest,
        fallback: _RuleBasedChainBuilder,
        complexity: ComplexityBand,
    ) -> tuple[list[ReasoningStep], ReasoningStrategy, list[str], list[str]]:
        """
        Returns (steps, strategy, sub_problems, flags).
        Falls back to rule-based on failure.
        """
        prompt = self._build_prompt(request)
        try:
            raw = await asyncio.wait_for(
                self._call_model(prompt),
                timeout=15.0,
            )
            return self._parse_response(raw, request)
        except Exception as exc:
            log.warning(
                "LLM reasoning failed, using rule-based fallback",
                error=str(exc),
                request_id=request.request_id,
            )
            strategy = self._default_strategy(complexity)
            steps = fallback.build(request, strategy, complexity)
            return steps, strategy, [], []

    async def _call_model(self, prompt: str) -> str:
        from models.providers.base_provider import ModelRequest, ModelMessage

        req = ModelRequest(
            messages=[
                ModelMessage(role="system", content=self._SYSTEM_PROMPT),
                ModelMessage(role="user", content=prompt),
            ],
            model="default",
            max_tokens=1024,
            temperature=0.2,  # low temperature for deterministic reasoning
        )
        response = await self._model.complete(req)
        return response.content

    def _build_prompt(self, request: ReasoningRequest) -> str:
        parts = [f"Problem: {request.raw_input}"]
        if request.intent:
            parts.append(f"Intent: {request.intent}")
        if request.domain != "general":
            parts.append(f"Domain: {request.domain}")
        if request.context_facts:
            parts.append(
                "Known facts:\n" + "\n".join(f"- {f}" for f in request.context_facts)
            )
        if request.constraints:
            parts.append(
                "Constraints:\n" + "\n".join(f"- {c}" for c in request.constraints)
            )
        return "\n\n".join(parts)

    def _parse_response(
        self,
        raw: str,
        request: ReasoningRequest,
    ) -> tuple[list[ReasoningStep], ReasoningStrategy, list[str], list[str]]:
        import json

        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        data = json.loads(cleaned)

        strategy_raw = data.get("strategy", "chain_of_thought")
        try:
            strategy = ReasoningStrategy(strategy_raw)
        except ValueError:
            strategy = ReasoningStrategy.CHAIN_OF_THOUGHT

        raw_steps = data.get("steps", [])
        step_ids: list[str] = []
        steps: list[ReasoningStep] = []

        for idx, rs in enumerate(raw_steps):
            try:
                kind = StepKind(rs.get("kind", "inference"))
            except ValueError:
                kind = StepKind.INFERENCE

            dep_indexes = rs.get("depends_on_indexes", [])
            depends_on = [
                step_ids[i]
                for i in dep_indexes
                if isinstance(i, int) and 0 <= i < len(step_ids)
            ]

            step = ReasoningStep(
                step_id=uuid.uuid4().hex[:8],
                index=idx,
                kind=kind,
                statement=str(rs.get("statement", "")),
                confidence=float(rs.get("confidence", 0.75)),
                depends_on=depends_on,
                evidence=rs.get("evidence", []),
                alternatives=rs.get("alternatives", []),
            )
            steps.append(step)
            step_ids.append(step.step_id)

        sub_problems: list[str] = data.get("sub_problems", [])
        flags: list[str] = data.get("flags", [])

        return steps, strategy, sub_problems, flags

    @staticmethod
    def _default_strategy(complexity: ComplexityBand) -> ReasoningStrategy:
        mapping = {
            ComplexityBand.TRIVIAL: ReasoningStrategy.CHAIN_OF_THOUGHT,
            ComplexityBand.SIMPLE: ReasoningStrategy.CHAIN_OF_THOUGHT,
            ComplexityBand.MODERATE: ReasoningStrategy.DECOMPOSITION,
            ComplexityBand.COMPLEX: ReasoningStrategy.DECOMPOSITION,
            ComplexityBand.UNKNOWN: ReasoningStrategy.HYPOTHESIS,
        }
        return mapping.get(complexity, ReasoningStrategy.CHAIN_OF_THOUGHT)


# ---------------------------------------------------------------------------
# ReasoningEngine — public interface
# ---------------------------------------------------------------------------


class ReasoningEngine:
    """
    Cognition layer entry point.

    Lifecycle
    ---------
    engine = ReasoningEngine()
    engine.inject(event_bus=bus, model_router=router)   # called by bootstrap
    await engine.start()
    ...
    result = await engine.reason(request)
    ...
    await engine.stop()

    Event contracts
    ---------------
    Subscribes:
        "reasoning.request"   → payload must be a ReasoningRequest.to_dict()-like dict
        "reflection.feedback" → payload: {"request_id": str, "adjustments": dict}

    Emits:
        "reasoning.started"   → payload: {request_id, intent, domain}
        "reasoning.result"    → payload: ReasoningResult.to_dict()
        "reasoning.failed"    → payload: {request_id, error}
    """

    MIN_RELIABLE_CONFIDENCE = 0.60
    # Use LLM only for MODERATE and above to avoid unnecessary API calls
    _LLM_COMPLEXITY_THRESHOLD = {ComplexityBand.MODERATE, ComplexityBand.COMPLEX}

    def __init__(self) -> None:
        self._event_bus: Any = None
        self._model_router: Any = None
        self._embedding_service: Any = None
        self._running: bool = False

        self._rule_builder = _RuleBasedChainBuilder()
        self._llm_builder: _LLMChainBuilder | None = None

        # Feedback loop: reflection engine can push confidence adjustments
        # keyed by domain — applied to future results in that domain
        self._confidence_adjustments: dict[str, float] = {}

        # History: lightweight ring buffer of recent results for introspection
        self._history: list[ReasoningResult] = []
        self._history_max = 100

        log.info("ReasoningEngine initialised")

    # ------------------------------------------------------------------
    # Dependency injection (called by bootstrap / DI container)
    # ------------------------------------------------------------------

    def inject(
        self,
        event_bus: Any = None,
        model_router: Any = None,
        embedding_service: Any = None,
    ) -> None:
        """Inject runtime dependencies. Call before start()."""
        if event_bus is not None:
            self._event_bus = event_bus
        if model_router is not None:
            self._model_router = model_router
            self._llm_builder = _LLMChainBuilder(model_router)
            log.info("ReasoningEngine: model_router attached — LLM reasoning enabled")
        else:
            log.info("ReasoningEngine: no model_router — rule-based reasoning only")
        if embedding_service is not None:
            self._embedding_service = embedding_service
            log.info("ReasoningEngine: EmbeddingService attached — semantic context ranking enabled")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._wire_subscriptions()
        log.info("ReasoningEngine started")

    async def stop(self) -> None:
        self._running = False
        log.info("ReasoningEngine stopped", results_produced=len(self._history))

    # ------------------------------------------------------------------
    # Primary interface
    # ------------------------------------------------------------------

    async def reason(self, request: ReasoningRequest) -> ReasoningResult:
        """
        Process a ReasoningRequest and return a ReasoningResult.

        This is the primary entry point for the DecisionEngine and any
        other cognition module that needs structured reasoning output.
        Never raises — returns a low-confidence result on internal error.
        """
        t_start = time.monotonic()
        await self._emit(
            "reasoning.started",
            {
                "request_id": request.request_id,
                "intent": request.intent,
                "domain": request.domain,
                "session_id": request.session_id,
            },
        )

        try:
            result = await self._process(request)
        except Exception as exc:
            log.error(
                "ReasoningEngine.reason failed",
                request_id=request.request_id,
                error=str(exc),
            )
            await self._emit(
                "reasoning.failed",
                {
                    "request_id": request.request_id,
                    "error": str(exc),
                },
            )
            result = self._error_result(request, str(exc))

        result.elapsed_ms = (time.monotonic() - t_start) * 1000

        self._store_history(result)
        await self._emit("reasoning.result", result.to_dict())

        log.info(
            "Reasoning complete",
            request_id=request.request_id,
            strategy=result.strategy.value,
            complexity=result.complexity.value,
            confidence=result.confidence,
            steps=len(result.chain),
            elapsed_ms=result.elapsed_ms,
        )

        return result

    async def reason_from_dict(self, data: dict[str, Any]) -> ReasoningResult:
        """
        Convenience wrapper — builds a ReasoningRequest from a raw dict
        (e.g. from an EventBus payload) and calls reason().
        """
        request = ReasoningRequest(
            request_id=data.get("request_id", uuid.uuid4().hex[:10]),
            raw_input=data.get("raw_input", data.get("input", "")),
            intent=data.get("intent", ""),
            domain=data.get("domain", "general"),
            context_facts=data.get("context_facts", []),
            constraints=data.get("constraints", []),
            priority=data.get("priority", 2),
            session_id=data.get("session_id"),
            correlation_id=data.get("correlation_id"),
            metadata=data.get("metadata", {}),
        )
        # Auto-parse intent / domain if not provided by caller
        if not request.intent or request.domain == "general":
            intent, domain, _ = _InputParser.parse(request.raw_input)
            request.intent = request.intent or intent
            request.domain = domain if request.domain == "general" else request.domain

        return await self.reason(request)

    # ------------------------------------------------------------------
    # Internal processing
    # ------------------------------------------------------------------

    async def _process(self, request: ReasoningRequest) -> ReasoningResult:
        # 1. Parse intent + domain + complexity if not already set
        if not request.intent:
            request.intent, request.domain, complexity = _InputParser.parse(
                request.raw_input
            )
        else:
            _, _, complexity = _InputParser.parse(request.raw_input)

        # 2. Choose builder — LLM for complex/moderate, rules otherwise
        use_llm = (
            self._llm_builder is not None
            and complexity in self._LLM_COMPLEXITY_THRESHOLD
        )

        if use_llm:
            steps, strategy, sub_problems, flags = await self._llm_builder.build(
                request, self._rule_builder, complexity
            )
        else:
            strategy = self._select_strategy(complexity, request.domain)
            steps = self._rule_builder.build(request, strategy, complexity)
            sub_problems = self._detect_sub_problems(steps)
            flags = self._detect_flags(request, steps)

        # 3. Apply reflection feedback (domain-level confidence adjustment)
        adjustment = self._confidence_adjustments.get(request.domain, 0.0)
        if adjustment != 0.0:
            steps = self._apply_confidence_adjustment(steps, adjustment)

        # 4. Compute aggregate confidence
        conclusion_step = next(
            (s for s in reversed(steps) if s.kind == StepKind.CONCLUSION),
            steps[-1] if steps else None,
        )
        agg_confidence = conclusion_step.confidence if conclusion_step else 0.5
        conclusion_text = (
            conclusion_step.statement if conclusion_step else "No conclusion reached."
        )

        return ReasoningResult(
            result_id=uuid.uuid4().hex[:10],
            request_id=request.request_id,
            strategy=strategy,
            complexity=complexity,
            chain=steps,
            conclusion=conclusion_text,
            confidence=agg_confidence,
            sub_problems=sub_problems,
            flags=flags,
        )

    def _select_strategy(
        self,
        complexity: ComplexityBand,
        domain: str,
    ) -> ReasoningStrategy:
        if complexity == ComplexityBand.TRIVIAL:
            return ReasoningStrategy.CHAIN_OF_THOUGHT
        if domain == "coding":
            return ReasoningStrategy.DECOMPOSITION
        if domain == "research":
            return ReasoningStrategy.HYPOTHESIS
        if complexity == ComplexityBand.COMPLEX:
            return ReasoningStrategy.DECOMPOSITION
        return ReasoningStrategy.CHAIN_OF_THOUGHT

    def _detect_sub_problems(self, steps: list[ReasoningStep]) -> list[str]:
        """Extract potential sub-problems from assumption steps."""
        return [
            step.statement
            for step in steps
            if step.kind == StepKind.ASSUMPTION and step.confidence < 0.75
        ]

    def _detect_flags(
        self,
        request: ReasoningRequest,
        steps: list[ReasoningStep],
    ) -> list[str]:
        flags: list[str] = []
        if len(request.raw_input.split()) < 4:
            flags.append("ambiguous_intent")
        if not request.context_facts:
            flags.append("no_context_provided")
        low_conf = [s for s in steps if s.confidence < 0.65]
        if len(low_conf) > len(steps) // 2:
            flags.append("low_chain_confidence")
        if request.priority == 1:
            flags.append("critical_priority")
        return flags

    @staticmethod
    def _apply_confidence_adjustment(
        steps: list[ReasoningStep],
        adjustment: float,
    ) -> list[ReasoningStep]:
        """Clamp-adjusted confidence scores based on reflection feedback."""
        adjusted: list[ReasoningStep] = []
        for step in steps:
            new_conf = max(0.0, min(1.0, step.confidence + adjustment))
            adjusted.append(
                ReasoningStep(
                    step_id=step.step_id,
                    index=step.index,
                    kind=step.kind,
                    statement=step.statement,
                    confidence=new_conf,
                    depends_on=step.depends_on,
                    evidence=step.evidence,
                    alternatives=step.alternatives,
                    metadata=step.metadata,
                )
            )
        return adjusted

    @staticmethod
    def _error_result(request: ReasoningRequest, error: str) -> ReasoningResult:
        error_step = ReasoningStep(
            step_id=uuid.uuid4().hex[:8],
            index=0,
            kind=StepKind.CONCLUSION,
            statement=f"Reasoning failed: {error}",
            confidence=0.0,
        )
        return ReasoningResult(
            result_id=uuid.uuid4().hex[:10],
            request_id=request.request_id,
            strategy=ReasoningStrategy.CHAIN_OF_THOUGHT,
            complexity=ComplexityBand.UNKNOWN,
            chain=[error_step],
            conclusion=f"Reasoning failed: {error}",
            confidence=0.0,
            flags=["reasoning_error"],
        )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _store_history(self, result: ReasoningResult) -> None:
        self._history.append(result)
        if len(self._history) > self._history_max:
            self._history.pop(0)

    def get_history(
        self,
        limit: int = 10,
        domain: str | None = None,
    ) -> list[ReasoningResult]:
        results = self._history[-limit:]
        if domain:
            results = [
                r
                for r in results
                if any(domain in s.metadata.get("domain", "") for s in r.chain)
            ]
        return results

    def get_stats(self) -> dict[str, Any]:
        if not self._history:
            return {"total": 0}
        confidences = [r.confidence for r in self._history]
        strategies = {}
        for r in self._history:
            strategies[r.strategy.value] = strategies.get(r.strategy.value, 0) + 1
        return {
            "total": len(self._history),
            "avg_confidence": round(sum(confidences) / len(confidences), 3),
            "min_confidence": round(min(confidences), 3),
            "max_confidence": round(max(confidences), 3),
            "strategy_breakdown": strategies,
            "llm_enabled": self._llm_builder is not None,
            "running": self._running,
        }

    # ------------------------------------------------------------------
    # EventBus wiring
    # ------------------------------------------------------------------

    def _wire_subscriptions(self) -> None:
        if self._event_bus is None:
            log.warning("ReasoningEngine: no event_bus — event subscriptions skipped")
            return
        self._event_bus.subscribe("reasoning.request", self._on_reasoning_request)
        self._event_bus.subscribe("reflection.feedback", self._on_reflection_feedback)
        log.debug("ReasoningEngine subscriptions registered")

    async def _on_reasoning_request(self, event: Any) -> None:
        """Handle incoming reasoning.request events from other modules."""
        try:
            await self.reason_from_dict(event.payload)
        except Exception as exc:
            log.error("ReasoningEngine: _on_reasoning_request failed", error=str(exc))

    async def _on_reflection_feedback(self, event: Any) -> None:
        """
        Receive feedback from the ReflectionEngine.
        Expected payload: {"domain": str, "confidence_delta": float, "adjustments": dict}
        """
        payload = event.payload
        domain = payload.get("domain", "general")
        delta = float(payload.get("confidence_delta", 0.0))
        # Decay existing adjustment and blend with new signal
        existing = self._confidence_adjustments.get(domain, 0.0)
        blended = round(existing * 0.7 + delta * 0.3, 4)
        self._confidence_adjustments[domain] = blended
        log.info(
            "ReasoningEngine: confidence adjustment applied",
            domain=domain,
            delta=delta,
            blended=blended,
        )

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            from kernel.event_bus.event_bus import Event

            await self._event_bus.publish(
                Event(
                    event_type=event_type,
                    source="cognition.reasoning_engine",
                    payload=payload,
                )
            )
        except Exception as exc:
            log.debug(
                "ReasoningEngine: emit failed", event_type=event_type, error=str(exc)
            )
