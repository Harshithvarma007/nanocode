# Demo projects

Used in the video's cold open and the final "real test". Reset them before every take:

```bash
./demo/reset.sh
```

## 1. `demo/shop` - fix the failing tests

A small shopping cart with three planted bugs (quantity ignored in the subtotal, coupon
percentage not divided by 100, wrong exception type) and six unit tests, four of them failing.

```bash
cd demo/shop
python -m unittest -v        # 4 failures
nanocode                     # then type:
```

> The tests are failing. Find out why and fix the code (not the tests). Run the tests to prove it.

It works well with local models (qwen3:8b) and is a good side-by-side with Claude.

## 2. `demo/habits` - build something from nothing

Start in an empty folder (`mkdir -p demo/habits && cd demo/habits`), switch to plan mode
(shift+tab twice) and ask:

> Build a command-line habit tracker in Python with no dependencies: `python habits.py add <name>`,
> `python habits.py done <name>` (marks today), and `python habits.py list` (shows each habit and its
> current streak). Store data in habits.json. Include unit tests and a short README. Run the tests.

Approve the plan with "Yes, and auto-accept edits" and watch it build, test and fix itself.
