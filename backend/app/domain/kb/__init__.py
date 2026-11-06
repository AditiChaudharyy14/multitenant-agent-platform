"""Knowledge base: markdown in seed/<slug>/kb → chunks → embeddings → hybrid search.

The KB holds descriptive, slow-changing text only. Live facts (prices, stock, order
status, availability) come from database tools; lint.py enforces that at seed time.
"""
