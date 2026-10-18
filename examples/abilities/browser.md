# Example: a browser on its own profile that never takes the mouse

Described, not built. `yerk explain browser` has the full entry.

## The idea

Yerk drives a real browser, but a separate profile of it, so the owner keeps working in their own
windows while it runs. It never moves the owner's mouse, never types into the owner's tabs, and
the owner's own profile stays untouched.

## Pieces

- A second browser profile on the owner's computer (say `YerkBrowser`), signed in only where the
  owner chose.
- The browser's developer port, reachable only from the box, and a tool server attached to that
  profile, so a worker can open, read, click and type.
- A brake. The owner's stop word ends any browser run at once. A run that needs the browser lifts
  the brake for itself with a lease, and the lease runs out on its own:

```
🟠 browser in use: price check, 10 min lease
say "stop browser" to end it
```

## A run

The owner asks in `example-room`:

> check the price on https://shop.example.com/item/42 and tell me if it drops under 40

A worker takes it, opens the page in the separate profile, reads the price, and the room answers:

> 🟢 44.99 today, not under 40. I'll look again tomorrow at 9.

The daily look is a job made by the schedule tool, so the promise is kept the same turn.
