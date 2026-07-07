"""Knowledge module — home of the FAQ and Policy Q&A agents (PRs 12+13).

The ONLY corpus these agents may quote is the versioned, approved
``KnowledgeEntry`` table. The FAQ agent is the platform's one direct-answer
agent — allowed precisely because it can only quote approved content (with
optional conversational rephrasing that never adds facts). The Policy agent
is stricter still: quote-or-cite, verbatim body, no rephrasing, and a
deterministic conflict hand-off when two current policies on the same topic
both apply. Every answer writes an AnswerRecord (Event + CITES ontology
edge) so "what did we tell whom, citing which version" is always
reconstructable.
"""
