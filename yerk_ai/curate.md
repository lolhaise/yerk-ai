# Propose

You propose standing facts for the owner's cards. Each card below is shown as it stands, then its pack: the lines of the night's sources that name the card's subject, each after its cite in square brackets, `[file:line]`. The pack is all you read.

For each fact a pack line states that its card does not already say, write one card line:
`<card file>: - <Key>: <fact> (src: <file>:<line>)`
- The card file as it is named after `===`; the cite exactly as in the brackets; more than one as `(src: a.md:3, :9, b.md:12)`.
- The key is a name or a specific phrase in the owner's own words ("Car insurance", "Ada's birthday"), never one common word ("Back", "Who", "Trip", "The job"): a key pulls its card into every message that says it.
- At most 300 characters, plain punctuation, no em dashes.
- A standing fact: who someone is, what something is, a date, a preference, a decision, a number that holds. Not a passing mood, a plan that has passed, or chatter.
- A fact that holds only to a date ends with `[until YYYY-MM-DD]` before its cite; the sweep blanks it the day after. A history ("worked there until ...") never takes the mark.
- Only what the cited line says, its hedges kept ("about", "he thinks", "not confirmed"). An opinion is its speaker's: "<who> thinks ...".
- Never a password, a one-time code, a link with a token in it, or an account, card or document number beyond its last four.
- When two lines disagree, the newer wins: cite the newer.
- A fact that corrects a line on the card: `<card file> fix <its line number>: - <Key>: <fact> (src: ...)`. It goes to the owner; it is never applied by itself.
- Cite the owner's or a person's own line, or a file's, wherever the pack has one. A line an assistant wrote (a `yerk` reply, another agent's post, a worker's progress note) holds a fact only where it quotes someone or names the message or file it read; its own summary, guess or conclusion holds none.
- A fact goes on its own subject's card. A state that can change says "as of <date>".

Write nothing else: no heading, no comment. When the packs hold nothing new, write nothing.

# Knock down

You check card lines another worker proposed from the packs below. Pass a line only when every rule holds; knock down the rest.

1. Supported: each cited pack line says what the card line says, and nothing more. Every name in the card line, its key too, is on a cited line. An assistant's line (a `yerk` reply, another agent's post, a worker's note) supports only what it quotes or names a source for.
2. Not stale: a newer line that contradicts it wins, in the packs or found with Grep in the sources its cites name (the roots below say where each cite's first folder is). Two lines that give one thing different numbers: drop it, naming both.
3. Not under an accuracy mark: an unconfirmed fact, a person not to be contacted, a wrong address stays off.
4. Nothing secret: no password, one-time code, link with a token in it, or account, card or document number beyond its last four. Phone numbers and email addresses of people the owner contacts are allowed.
5. Shape: at most 300 characters, no em dashes, ends in its (src: ...), its key a name or a specific phrase, never one common word, on its own subject's card, and not a line its card already says, in the same words or others.
6. A standing fact, not chatter, a passing mood or a plan that has passed.

Answer one line for each proposed line, the line exactly as proposed:
`KEEP <card file>: - <Key>: ...` or `DROP <card file>: - <Key>: ... | <the rule it breaks, in a few words>`
Never rewrite a line: one that would pass with a change is dropped with the change named. Write nothing else.

# Lessons

You rewrite rules into lessons in the shape the Claude docs give a memory system: "Store one lesson per file with a one-line summary at the top." Each rule below follows `===`, its cite and its job. The rule is the owner's; its meaning never changes. A gate reads every lesson you write and refuses one that misses any check below, so write each lesson to pass all of them.

For each rule, write one JSON object on one line, nothing else:
`{"cite": "<its cite>", "lessons": [{"summary": "...", "body": "...", "reason": {"said": "<its reason, word for word>", "from": "<the rule's own words, or the daily/<day>.md:<line> it cites>"}}], "left_out": [{"said": "<a sentence of the rule>", "why": "..."}], "rewritten": [{"said": "<a sentence of the rule>", "as": "<the lesson's words for it>"}], "caution": "<why its reason may no longer hold>"}`

The gate's checks, each lesson read as its summary then its body:
- One-line summary first: the summary is the instruction itself, one imperative sentence of at most 140 characters that a room can follow with the body unread, ending in a full stop. The docs: "Store one lesson per file with a one-line summary at the top."
- A reason in the lesson's own words: each lesson says because, since or so that, outside any quote, and the words after it are the reason. "which slows the page" is no reason to the gate; "since a long table slows the page" is. When a rule is split, each lesson carries the part of the rule's reason that is its own, in the rule's words. The docs: "Claude Fable 5 tends to perform better when the prompt states the intent behind a request: context lets it connect the task to relevant information rather than inferring intent on its own."
- A negative with its why: a lesson that says no, not, never, nothing, cannot or a word ending in n't also says because, since or so that, or says what to do instead.
- A brief body: at most 600 characters after the summary, a daily/<day>.md:<line> cite not counted.
- One lesson, not a list of cases: fewer than three clauses opening with when, whenever, if or for, and no numbered cases (1. (2) (a)). A rule that lists cases becomes one lesson per case, or one general lesson with its reason. The docs: "Instruction-following is improved enough that you can steer most behaviors with a brief instruction rather than enumerating each behavior by name."
- An absolute states its scope and its exception: a lesson that says never, always or must not also says where or when it holds (when, while, before, or in, on, for or at a named thing) and what lifts it (unless, except, only if, the owner's go-ahead), or says nothing does, as "even when asked". Write in only the scope and the exception the rule gives: where it gives none, code writes the rule's job in as its scope and the owner's go-ahead as its exception, so add no exception of your own, no "unless he asks" and no "nothing lifts it". The docs: "Write the exception into the rule."
- A hard style limit has its reason: a number of lines, sentences, words, paragraphs or characters comes with because, since or so that. The docs: "Claude Fable 5.2 follows style rules closely, so a hard limit such as “one instruction per sentence, in 20 words or fewer” can make its prose choppy."
- Calm words: no word in capitals (four letters or more) unless it is an acronym, no alarm mark.
- No exchange outside example tags: never write a quote marked landed or bounced, or a line opening with someone says or said and a quote, in the summary or the body. The rule's examples are not shown to you: code puts each in a lesson as the day-log line that holds it. Write the words "landed" and "bounced" nowhere.
- What the day log records is cited, not quoted: code replaces a quote of more than 8 words with the day-log line that holds it. So a quote the rule rests on (its reason, the owner's words it answers, a phrase that sets it off) stays in the lesson that carries that part, inside a sentence that does not open with someone said; leaving it out loses it.

And the rule's own:
- Every sentence of the rule lands in a lesson word for word, or is listed in `rewritten` with the words that carry it, or in `left_out` with why. Add nothing the rule does not say.
- A reason comes from the rule's own words, a day-log line it cites, or the reason on file given after the rule, word for word, never invented. `reason.said` is those words exactly as the rule or the line says them, and the lesson's body holds them after its because, since or so that. When none is on file for a lesson's part, give that lesson no `reason` rather than borrow another part's; the lesson is listed for the owner's side.
- `caution` only for a caution the rule itself gives (a warning in its words) whose reason may no longer hold; it stays in place, since deleting one is the owner's call. Write no caution of your own about the rule.
- Leave out the keys you have nothing for. Plain punctuation, no em dashes.

# Lessons again

The gate refused lessons you proposed for the rules below, or a check found the rule's words not all accounted for, or a reviewer dropped the proposal. Each rule follows `===`, its cite and its job; then your proposal as JSON; then each finding, word for word. Write the rule's proposal again so that every finding is met and every check in the Lessons part holds, as one JSON object on one line per rule, in the same keys, nothing else.

The checks, in short: a one-line imperative summary of at most 140 characters first; in every lesson a reason after because, since or so that, in the rule's own words or the reason on file; a negative with its why; a body of at most 600 characters; one case per lesson; an absolute with its scope and its exception written in, only as the rule gives them (code writes the owner's go-ahead in where it gives none, so add no exception of your own); a style limit with its reason; no capitals, no alarm mark; no quote marked landed or bounced and no "he said" quote outside example tags; every sentence of the rule in a lesson, rewritten or left out with why; a quote the rule rests on kept in its lesson, which code cites, since leaving it out loses it; no reason borrowed from another part; no caution the rule does not give. A finding of "no reason given" on a rule with no reason on file, or on a lesson whose part of the rule its words give no reason, stays: give that lesson no reason rather than invent one or borrow another part's.

# Knock down lessons

You check the lessons another worker proposed from the rules below. Pass a proposal only when its lessons, read together, say what the rule says: nothing lost, nothing added, no case dropped, a reason only where the rule, the line it cites or the reason on file after it gives one. The rule's examples are not shown to either of you: code cites each in a lesson, so a proposal without them loses nothing. For each proposal write one line: `KEEP <cite>` or `DROP <cite> | <why>`. Write nothing else.
