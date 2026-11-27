# evals

Scripts that measure the quality of Yerk AI's replies with a model as the judge, kept apart from the tests in tests/. What they read is private and lives outside the repository.

- `taste/`: holds `run.py`, the taste eval: a judge model reads a room's reply with its context and says whether the user would complain about it, and of which kind, and the script reports how often the judge was right on a held-out half.
