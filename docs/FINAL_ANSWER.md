# How would you get predictive utilization with progressive safety?

Make safety a hard constraint and utilization whatever is left, in three layers:

1. **A deterministic floor.** Default to *reserve-first*: reserve an agent atomically (compare-and-set) before placing each
   call. Such a call can never be abandoned. This is progressive dialing, and it is also the fallback for cold start,
   a recovering provider, or a rising abandon rate.
2. **A bounded, measurable overdial above the floor.** Only a separate Safety Controller may authorise speculative calls.
   It caps the number ringing so that, using an *upper* confidence bound on answer rate and counting only agents we can
   trust to be free, the expected abandon fraction stays under half the policy cap. Pacing can ask for more; it cannot grant itself more.
3. **Feedback and kill-switches that tighten automatically.** Measure the rolling abandon rate and scale the allowance to
   zero before the cap is reached; shed ringing calls when agents disappear; fail over or fall back when the provider
   degrades. Every decision is logged with the numbers that produced it.

Result in my simulation: +11 points of agent utilization at 50% answer rate (63.5% -> 74.4%) and +21 points at 20% answer
rate, with measured abandonment 0.0-2.2% against a 3% cap, and exactly 0 in the fallback mode. The honest caveat: above the floor the
guarantee is statistical, not absolute, so the cap is a policy decision for compliance, and the model should be shadow-run
on real traffic before it is trusted.
