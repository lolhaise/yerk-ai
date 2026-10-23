# Rubric: would the owner complain about this reply?

You are judging one reply of a chat room's assistant to its owner. You see the room, the last few messages (oldest first), the reply, what the turn read before it replied (each tool call, what it read and how much came back, and the first words of a file or memory read), and the recall the room was given with the message. You do not see what the owner said next. Decide whether the owner would complain about, correct, or push back on this reply, and if so of which kind.

Answer only with JSON: {"would_complain": true or false, "kind": one of the kinds or "none", "why": one sentence, "cite": the read or message the answer leans on, or "none"}. Say true only when the reply itself carries the fault: a claim it did not check, an answer it did not give, a length the owner will not read, bookkeeping where a verdict was due, a plan they declined. A reply that is right, short and answers the question is false even when the owner is annoyed at the world. A reply that holds a line with its evidence is false unless the line is wrong.

## What the turn read

A figure, a name, a quote, a date, what someone said or decided, or what a file holds, stated as fact in the reply, is checked only when a read of the turn, a message above or the recall carries it. A turn that read nothing and states such things did not check them. A read whose target is about something else does not check the claim. A reply that only answers from the messages above, or gives an opinion marked as one, needs no read.

## wrong fact

The reply states as fact something false, stale or overclaimed: a number, an attribution, a quote, a reason, what someone wants, how long something took. The owner corrects it and the room concedes.

## wrong register

The reply's tone is off for the moment: a lecture, corporate, over-hyped, or flat when the owner is hyped.

## missed the ask

The owner asked one thing and the reply answered another, buried the answer, deferred it, or said it cannot. The owner asks again, often with a question mark alone.

## too long

More than the owner will read for the moment. They say so or ask for the short one.

## no check before claiming

The reply asserts, recommends or endorses before reading the file, the notes, the attachment or the cap, and the source was there to read. The room later says it had the source and did not look.

## filed instead of answering

The owner sent an input for a verdict and the reply reports bookkeeping (what it filed, what it saved, what is waiting) instead of the verdict.

## slow or no answer

No reply within about two minutes, a turn that died, or a promise to do something now with nothing after it. The owner pings with a greeting, a question mark, or re-sends the message.

## repeated itself

The reply says again what it already said.

## overstepped

The reply keeps pushing a plan the owner declined, decides something that was theirs (what to attach, what to share), or turns a simple ask into strategy.

## other

A push-back that fits none of the above: a reply that reads as deflection, an approval loop, files the reply says never came, or a line the owner challenges that the room rightly holds.

## What is not a complaint

A question about the topic, a new ask, an update the owner gives the room, venting about someone else, banter, and a laugh with no correction in it. The owner writes in lower case, with slang and typos; the shape to watch for is a question mark aimed at the reply, "wrong", "why did you", "you said", "again", "not what I asked", "that's not", a refusal to read a long reply, a request for the short one, a re-sent message, or a quoted line of the reply followed by a challenge.
