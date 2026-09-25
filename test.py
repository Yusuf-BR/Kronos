from core.session import ConversationSession, MAX_HISTORY_TURNS

session = ConversationSession(retriever=None, synthesizer=None, session_id="test")
for i in range(MAX_HISTORY_TURNS + 5):
    session.turns.append({"question": f"q{i}", "mentions": [], "linked_entities": [], "answer": ""})

print(f"Stored turns: {len(session.turns)} (cap: {MAX_HISTORY_TURNS})")
print(f"Oldest remaining question: {session.turns[0]['question']}")