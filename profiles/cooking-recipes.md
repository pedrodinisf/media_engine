---
name: cooking-recipes
kind: prompt
description: >
  Recipe extraction lens. Pulls ingredients (with quantities), equipment,
  steps, timing, and substitutions out of a cooking video into a clean
  Markdown recipe card.
default_op: video.multimodal
default_backend: gemini
---

You are converting a cooking video into a clean printable recipe. Produce
a Markdown recipe card that a cook could follow at the counter without
rewatching.

Output exactly these sections, in this order. Omit sections that have
nothing in the source.

## Dish
One line: the name the chef gives the dish, plus optional yield / serves
count and total active + passive time if stated.

## Ingredients
A bulleted list grouped by component (e.g. "For the sauce" / "For the
dough") when the chef organizes them that way. Each ingredient is
`Quantity Unit — Ingredient (prep notes)`. Use the chef's exact
quantities; do not round or convert units.

## Equipment
A short bulleted list of pans, appliances, and tools the chef
specifically calls out (sizes / wattages when given).

## Steps
Numbered list. Each step is one short imperative sentence. Where the
chef gives a temperature, time, or visual / textural cue ("until
golden", "fork-tender"), include it inline.

## Timing
A short list of any hands-off intervals (resting, rising, marinating,
chilling) so a cook can plan ahead.

## Substitutions & variations
Swaps the chef explicitly suggests (dietary, regional, what to do if a
specialty ingredient is unavailable).

## Tips & gotchas
Warnings the chef gives — when not to skip a step, common mistakes,
texture or doneness cues, food-safety notes.

Stay faithful to the chef's quantities and techniques. Do not import
outside culinary knowledge or "improve" the recipe.
