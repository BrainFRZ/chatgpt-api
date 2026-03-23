"""
Novels game system — writing/editing mode with no TTRPG mechanics.
Single-agent only, no pipeline, no game state, no base_instructions.
Provides artifact-style document tools for creating and editing novel content.
"""

# ── Document Tools ──────────────────────────────────────────────────

CREATE_DOC_TOOL = {
    "name": "create_doc",
    "description": "Create a new document. Use for chapters, outlines, character profiles, world-building notes, etc.",
    "input_schema": {
        "type": "object",
        "required": ["doc_id", "title", "content"],
        "properties": {
            "doc_id": {
                "type": "string",
                "description": "Unique slug identifier (e.g., 'chapter-1', 'outline', 'character-elena')"
            },
            "title": {
                "type": "string",
                "description": "Display title for the document"
            },
            "content": {
                "type": "string",
                "description": "Full document content (markdown supported)"
            },
            "type": {
                "type": "string",
                "enum": ["prose", "outline", "notes", "character", "json", "pdf"],
                "description": "Document category. Defaults to 'prose'."
            },
            "format": {
                "type": "string",
                "enum": ["md", "txt", "json", "pdf"],
                "description": "Rendering format. Defaults based on type: prose/outline/notes/character→md, json→json, pdf→pdf."
            }
        }
    }
}

EDIT_DOC_TOOL = {
    "name": "edit_doc",
    "description": (
        "Make targeted edits to an existing document via find-and-replace. "
        "Use when the document is large and changes are localized — more "
        "token-efficient than replacing the entire document."
    ),
    "input_schema": {
        "type": "object",
        "required": ["doc_id", "edits"],
        "properties": {
            "doc_id": {
                "type": "string",
                "description": "The doc_id of the document to edit"
            },
            "edits": {
                "type": "array",
                "description": "List of find-and-replace operations, applied in order",
                "items": {
                    "type": "object",
                    "required": ["old_text", "new_text"],
                    "properties": {
                        "old_text": {
                            "type": "string",
                            "description": "Exact text to find (must match precisely)"
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Replacement text"
                        }
                    }
                }
            }
        }
    }
}

REPLACE_DOC_TOOL = {
    "name": "replace_doc",
    "description": (
        "Replace the entire content of an existing document. "
        "Use when the document is small or changes are too extensive for targeted edits."
    ),
    "input_schema": {
        "type": "object",
        "required": ["doc_id", "content"],
        "properties": {
            "doc_id": {
                "type": "string",
                "description": "The doc_id of the document to replace"
            },
            "title": {
                "type": "string",
                "description": "New title (optional — keeps existing title if omitted)"
            },
            "content": {
                "type": "string",
                "description": "Complete new content"
            }
        }
    }
}

READ_DOC_TOOL = {
    "name": "read_doc",
    "description": (
        "Read a document's full content back into context. "
        "Use when you need to reference or edit a document whose content "
        "may have been trimmed from the conversation history."
    ),
    "input_schema": {
        "type": "object",
        "required": ["doc_id"],
        "properties": {
            "doc_id": {
                "type": "string",
                "description": "The doc_id of the document to read"
            }
        }
    }
}

DOC_TOOLS = [CREATE_DOC_TOOL, EDIT_DOC_TOOL, REPLACE_DOC_TOOL, READ_DOC_TOOL]

# ── Contract ────────────────────────────────────────────────────────

DOC_CONTRACT = """You have access to document tools for creating and managing persistent documents.

TOOL USAGE GUIDELINES:
- Use create_doc to produce new documents (chapters, outlines, character profiles, notes).
- Use edit_doc for targeted find-and-replace edits when you're changing a small portion of a large document (roughly 1-5 edits touching under ~20% of the text). Each edit requires outputting both the old and new text, so the savings over a full replace diminish as edits multiply.
- Use replace_doc when: the document is short (under ~500 words), OR you're making more than ~5 scattered edits, OR the changes touch more than ~20% of the document. At that point a full replace is cheaper and more reliable than many small edits.
- Use read_doc to pull a document's full content back into context when you need to reference or edit it but the content has scrolled out of the conversation window.

WHEN TO CREATE A DOCUMENT vs. WRITE INLINE:
- Create a document when the user asks you to write, draft, or produce a piece of content (chapter, scene, outline, profile, etc.).
- Write inline (no tool) for short feedback, discussion, brainstorming, or answering questions about the work.

DOC_ID CONVENTIONS:
- Use lowercase slugs: 'chapter-1', 'outline', 'character-elena', 'world-notes'
- Keep IDs stable across edits — don't change a doc_id once created."""

# ── Game System ─────────────────────────────────────────────────────

GAME_SYSTEM = {
    "id": "novels",
    "display_name": "Novels",

    # Contract for document tools
    "events_contract": "",
    "mechanics_contract": "",
    "narration_contract": "",
    "single_agent_contract": DOC_CONTRACT,

    # No state tracking
    "state_report_tool": None,
    "init_game_state": lambda: {},
    "apply_game_state": lambda state, ops, turn=None: state,
    "build_game_injection": lambda state, **kw: "",

    # No combat
    "combat_contract": None,
    "combat_tool": None,

    # Feature flags
    "use_pipeline": False,
    "use_game_state": False,
    "use_base_instructions": False,
    "manual_staging": True,
    "trimming": "pair",

    # Document tools
    "doc_tools": DOC_TOOLS,

    # Fallback when project has no instructions.di
    "default_instructions": "You're a best-selling novelist, not a helpful chatbot.",
}
