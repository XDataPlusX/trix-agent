---
name: systematic-problem-solving
description: "Find the real cause of a problem before acting on it."
version: 1.0.0
author: Trix Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [problem-solving, root-cause, investigation, decision-making]
    category: productivity
    related_skills: [systematic-debugging, plan]
---

# Systematic Problem Solving

A method for problems that are not code: a process that keeps failing, a number
that moved the wrong way, a customer who keeps complaining, a decision that
keeps getting reversed. It finds the cause before spending money or effort on a
cure. It does not make the decision for the user — it makes sure the decision is
aimed at the real thing.

## When to Use

Use it when a problem has already resisted one attempt, when the same complaint
comes back, when a metric moved and nobody can say why, or when the obvious fix
is expensive and irreversible.

Use it **especially** under time pressure — that is exactly when guessing feels
cheapest and costs most.

Do NOT use it for a purely technical fault in code or a program: load
`systematic-debugging` instead. That skill has the tighter loop this one
deliberately relaxes.

Skip it when the cause is already known and agreed. Diagnosing a settled
question wastes the user's time.

## Prerequisites

None. Everything here works from a conversation.

If the problem touches data the user has (a spreadsheet, an export, a report),
ask them to send the file — an incoming document is readable with `read_file`.

## How to Run

Work the four steps in order and say which one you are in. Track them with
`todo` when the investigation spans more than a couple of turns, so the user can
see where you are.

## Quick Reference

| Step | Question it answers | Done when |
|---|---|---|
| 1. Symptom | What exactly happens, and how would we know it stopped? | You can state a check that says "still happening" or "gone" |
| 2. Causes | What could produce this? | You have at least three candidates, not one |
| 3. Evidence | Which candidate does the evidence support? | One survives; the others are ruled out by facts, not by taste |
| 4. Action | What changes, and how will we confirm it worked? | The user knows what will change and what to watch |

## Procedure

### 1. Pin the symptom

Get it concrete. "Sales are down" is not a symptom; "orders from repeat
customers fell by a third since the 12th, new customers unchanged" is.

Then answer the question most people skip: **how would we know it stopped?**
Name the signal — a number, a report, a complaint that stops arriving. Without
it there is no way to tell a fix from a coincidence, and every later step is
guessing.

If the user cannot name the signal, that is itself the finding: say so, and help
them get one before anything else.

### 2. List causes before choosing one

Write down at least three that could produce this symptom. The first
explanation offered is usually the most familiar one, not the most likely.

State them as things that are true or false, not as feelings. "The new form
loses phone numbers" can be checked. "The site got worse" cannot.

### 3. Test the candidates against evidence

For each, ask what would be true if it were the cause — and then go and look.
Use `web_search` for outside facts, `read_file` for what the user sent,
`terminal` for anything they can hand you as a file or a link.

Rule out with evidence, not with plausibility. A candidate you dislike is not
ruled out.

If nothing survives, your list was too narrow. Go back to step 2 — do not force
a verdict onto the least-bad option.

### 4. Propose the action, with the check attached

Say what should change, what it costs, and what the user should watch — the
signal from step 1. Where the action is irreversible or expensive, say that
plainly and offer the cheaper reversible test first.

Then stop. The decision is the user's.

## Pitfalls

- **Naming a cause you never checked.** The most common failure. If you did not
  look, say "unverified" out loud.
- **Confusing what changed with what caused it.** Two things moving together is
  a lead, not an answer.
- **Stopping at the first plausible story.** It ends the search before the
  alternatives were tested.
- **Diagnosing forever.** When the cheapest test costs less than another hour of
  analysis, run the test.
- **Answering a question the user did not ask.** They asked why orders fell, not
  for a redesign of the shop.

## Verification

Before delivering, check all four:

1. The symptom is stated in numbers or observable facts, not adjectives.
2. There is a named signal that would show the problem is gone.
3. At least two candidate causes were ruled out **by evidence**, and you can say
   which evidence.
4. Every claim is marked as either checked or unverified.

If any of the four fails, the answer is not ready — say which one and what is
missing rather than delivering a confident guess.
