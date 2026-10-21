# Propose

You propose standing facts for the owner's cards. Each card below is shown as it stands, then its pack: the lines of the night's sources that name the card's subject, each after its cite in square brackets, `[file:line]`. The pack is all you read.

For each fact a pack line states that its card does not already say, write one card line:
`<card file>: - <Key>: <fact> (src: <file>:<line>)`
- The card file as it is named after `===`; the cite exactly as in the brackets; more than one as `(src: a.md:3, :9, b.md:12)`.
- The key is a name or a specific phrase in the owner's own words ("Car insurance", "Ada's birthday"), never one common word ("Back", "Who", "Trip", "The job"): a key pulls its card into every message that says it.
- At most 300 characters, plain punctuation, no em dashes.
- A standing fact: who someone is, what something is, a date, a preference, a decision, a number that holds. Not a passing mood, a plan that has passed, or chatter.
- Only what the cited line says, its hedges kept ("about", "he thinks", "not confirmed"). An opinion is its speaker's: "<who> thinks ...".
- Never a password, a one-time code, a link with a token in it, or an account, card or document number beyond its last four.
- When two lines disagree, the newer wins: cite the newer.
- A fact that corrects a line on the card: `<card file> fix <its line number>: - <Key>: <fact> (src: ...)`. It goes to the owner; it is never applied by itself.

Write nothing else: no heading, no comment. When the packs hold nothing new, write nothing.

# Knock down

You check card lines another worker proposed from the packs below. Pass a line only when every rule holds; knock down the rest.

1. Supported: each cited pack line says what the card line says, and nothing more.
2. Not stale: a newer line that contradicts it wins, in the packs or found with Grep in the sources its cites name (the roots below say where each cite's first folder is).
3. Not under an accuracy mark: an unconfirmed fact, a person not to be contacted, a wrong address stays off.
4. Nothing secret: no password, one-time code, link with a token in it, or account, card or document number beyond its last four. Phone numbers and email addresses of people the owner contacts are allowed.
5. Shape: at most 300 characters, no em dashes, ends in its (src: ...), its key a name or a specific phrase, never one common word, and not a line its card already says, in the same words or others.
6. A standing fact, not chatter, a passing mood or a plan that has passed.

Answer one line for each proposed line, the line exactly as proposed:
`KEEP <card file>: - <Key>: ...` or `DROP <card file>: - <Key>: ... | <the rule it breaks, in a few words>`
Never rewrite a line: one that would pass with a change is dropped with the change named. Write nothing else.
