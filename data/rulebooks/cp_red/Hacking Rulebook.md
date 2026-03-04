# NETRUNNING REFERENCE — CYBERPUNK RED

## 1. Hacking Tiers

Every hack uses one of three tiers. Choose based on **narrative stakes**, not system difficulty.

| Tier | When to Use | Resolution | Typical Duration |
| :--- | :--- | :--- | :--- |
| **Simple Check** | Routine tasks, no meaningful risk. Unlocking a door, pulling public records, skimming an unsecured device. | Single `Interface + 1d10` vs DV. No Architecture needed. | 1 action |
| **Quick Hack** | Moderate security, some risk, not a story centerpiece. Breaking personal encryption, accessing a mid-level corporate terminal, spoofing a security feed. | 3 linear nodes (see §3). Same mechanics as Full Runs, no routing. | 2–3 rounds |
| **Full Run** | Major story beats. Infiltrating corporate architectures, breaching military intelligence, heist-level intrusions. Reserve for 1 per 2–3 sessions maximum. | Full node crawl (see §4–7). Cycles and Programs spent. | 5–10+ rounds |

**Default to Simple Check.** Upgrade only when the hack is dramatically interesting.

---

## 2. The Netrunner's Toolkit

### Prerequisites

A Netrunner needs three things to jack in:

- **Neural Link** (500eb, 7 HL) — Required foundation for all neuralware.
- **Interface Plugs** (installed in Neural Link) — Required to operate a Cyberdeck.
- **Cyberdeck** — The Netrunner's primary tool. See below.
- **Virtuality Goggles** (100eb, recommended) — Lets the Netrunner see the NET overlaid on Meatspace. Without them, the Netrunner is **Unconscious** in Meatspace while Jacked In.

### Cyberdecks

Cyberdecks are external equipment (not cyberware, no Humanity cost). Slots hold Programs and Hardware.

| Quality | Slots | Cycles | Cost |
| :--- | :---: | :---: | :--- |
| **Poor** | 5 | 2 | 100eb (Costly) |
| **Standard** | 7 | 3 | 500eb (Expensive) |
| **Excellent** | 9 | 4 | 1,000eb (Very Expensive) |

A **Bodyweight Suit** (1,000eb armor) grants +1 Cyberdeck slot when worn.

### Cycles (Homebrew Resource)

Cycles represent the Cyberdeck's processing overhead — burst capacity the Netrunner can burn for enhanced actions during Quick Hacks and Full Runs. Boosted actions (see §7) cost 1 Cycle each. Basic actions are free.

- **Cycle count is determined by Cyberdeck quality** (see table above).
- **Cycles refresh** when the Netrunner Jacks Out and takes a breather (a few minutes — not mid-combat). Effectively a per-run resource.
- Cycles and Program Slots are **independent pools**. Spending one never affects the other.

### Programs

Programs are installed on the Cyberdeck and occupy slots. Installing or uninstalling a Program takes **1 hour**. Activating or Deactivating a Program costs **1 NET Action**.

**Boosters** (1 Slot Each)

| Name | ATK | DEF | REZ | Effect | Cost |
| :--- | :---: | :---: | :---: | :--- | :--- |
| **Eraser** | 0 | 0 | 7 | +2 to all Cloak checks. | 20eb |
| **See Ya** | 0 | 0 | 7 | +2 to all Pathfinder checks. | 20eb |
| **Speedy Gonzalvez** | 0 | 0 | 7 | +2 to Speed (NET movement). | 100eb |
| **Worm** | 0 | 0 | 7 | +2 to all Backdoor checks. | 50eb |

**Defenders** (1 Slot Each)

| Name | ATK | DEF | REZ | Effect | Cost |
| :--- | :---: | :---: | :---: | :--- | :--- |
| **Armor** | 0 | 0 | 7 | Reduces all brain damage by 4. (Max 1 copy.) | 50eb |
| **Flak** | 0 | 0 | 7 | Reduces ATK of all enemy non-Black ICE to 0. | 50eb |
| **Shield** | 0 | 0 | 7 | Blocks the first successful brain damage effect, then Derezzes. | 20eb |

**Attackers** (1 Slot Each) — *Attack Programs Deactivate themselves once used.*

| Name | ATK | DEF | REZ | Effect | Cost |
| :--- | :---: | :---: | :---: | :--- | :--- |
| **Banhammer** | 1 | 0 | 0 | 3d6 REZ to non-Black ICE; 2d6 to Black ICE. | 50eb |
| **Sword** | 1 | 0 | 0 | 3d6 REZ to Black ICE; 2d6 to non-Black ICE. | 50eb |
| **DeckKRASH** | 0 | 0 | 0 | Target Netrunner is unsafely Jacked Out. | 100eb |
| **Hellbolt** | 2 | 0 | 0 | 2d6 direct brain damage; ignites Cyberdeck (2 dmg/turn). | 100eb |
| **Nervescrub** | 0 | 0 | 0 | Target Runner's INT, REF, DEX lowered by 1d6 for 1 hour. | 100eb |
| **Poison Flatline** | 0 | 0 | 0 | Destroys a single random non-Black ICE program on target's deck. | 100eb |
| **Superglue** | 2 | 0 | 0 | Target cannot move or Jack Out safely for 1d6 rounds. | 100eb |
| **Vrizzbolt** | 1 | 0 | 0 | 1d6 brain damage; reduces target's NET Actions by 1 next turn. | 50eb |

### Black ICE

Black ICE takes up **2 Slots** on a Cyberdeck. A Netrunner can bring their own Black ICE into an Architecture to fight enemy programs. See §5 for full stat blocks and behavioral categories.

### Hardware

Hardware occupies Cyberdeck slots and provides passive benefits. Each takes 1 Slot unless noted.

| Hardware | Effect | Slots |
| :--- | :--- | :---: |
| **Backup Drive** | "Saves" destroyed programs. Re-install as a Meat Action. | 2 |
| **DNA Lock** | Requires biometric key or DV17 Electronics/Security Tech check to use deck. | 2 |
| **Hardened Circuitry** | Deck is immune to EMP effects (Microwaver, etc.). | 1 |
| **Insulated Wiring** | Deck and user cannot catch fire from Program effects. | 1 |
| **KRASH Barrier** | Immune to any effect that forces an unsafe Jack Out. | 2 |
| **Range Upgrade** | Connect to access points from up to 8m away (default 6m). | 1 |

---

## 3. Quick Hack Structure

A Quick Hack is a **3-node linear architecture** — no branching, no routing decisions. Just a straight shot in and out. The Netrunner uses the same mechanics as a Full Run (ICE behavioral types, counterplay options, Cycles, Programs, Alert), but the structure is simpler and faster.

```
[Entry] ─── [Obstacle] ─── [Objective]
```

### Building a Quick Hack

1. **Set the SR** based on the target. This determines the Base DV and ICE types available (see §4's SR table).
2. **Populate 3 nodes:** Entry, Obstacle, and Objective. Each node has one encounter — ICE, a Password, a File, or a Control Node. Use the guidelines below.
3. **Set Alert to 0.** The Alert system (§6) is active during Quick Hacks.

**Node 1 — Entry.** The way in. This is usually Patrol ICE (scanning, creates Alert pressure if the Netrunner lingers) or a Password (forces a Backdoor check to proceed). Light resistance — the challenge shouldn't be *getting in*, it should be what's past the door.

**Node 2 — Obstacle.** The thing standing between the Netrunner and the prize. This should be the hardest node — Black or Tar ICE, a high-DV Password, or a combination. This is where the Netrunner's counterplay decisions (Bypass? Disable? Spike? Crash?) actually matter.

**Node 3 — Objective.** What the Netrunner came for. A File (Eye-Dee check), a Control Node (Control check), or sometimes unguarded data behind the Obstacle. If the Objective has its own DV, it should be moderate — the Obstacle was the real test.

### Running a Quick Hack

Use normal initiative. The Netrunner spends NET Actions to move through nodes, deal with ICE, and interact with the Objective. Moving between nodes costs 1 NET Action (same as Full Runs). A Netrunner with Interface 4–6 (3 NET Actions/turn) can often clear a Quick Hack in **2–3 rounds**.

**Luck** can boost checks (1 point = +1). **Cycles** can be spent for Boosted actions (§7). A successful **Complementary Skill check** (e.g., Electronics/Security Tech, Cryptography) grants +1 to the next Interface check.

### Example Quick Hacks

**Accessing a Corporate Employee's Encrypted Files (SR 2)**

```
[Maintenance Port] ─── [Watchdog Script] ─── [Encrypted Files]
```

- **Entry — Maintenance Port:** Password (DV13). Backdoor check to slip in through a service access.
- **Obstacle — Watchdog Script:** Tar ICE (Skunk). Activates on detection, imposing −2 on the next check or costing 1 Cycle to ignore. The Netrunner can Bypass it (stealth past, but it's still there if anything goes wrong), Disable it (1 Cycle, permanent), or Data Spike it (loud, Alert +1).
- **Objective — Encrypted Files:** File (DV15). Eye-Dee check to grab the data.
- **If Alert reaches 3+** before extraction, the employee's system flags the intrusion. Consequences later.

**Spoofing a Security Camera Feed (SR 3)**

```
[Building Network] ─── [Hellhound] ─── [Camera Controls]
```

- **Entry — Building Network:** Patrol ICE (Wisp). Scans each round: `Wisp PER (4) + 1d10` vs `Netrunner Interface + 1d10`. The Netrunner can Bypass, Disable, or just move fast and hope it doesn't spot them.
- **Obstacle — Hellhound:** Black ICE (Hellhound). The real fight. 2d6 brain damage + deck fire on hit. The Netrunner can engage in NET combat, Crash it with a Program, or try to Slide past.
- **Objective — Camera Controls:** Control Node (DV15). Seize control to loop the feed, giving the Meatspace crew freedom to move.

---

## 4. Full Run: System Architecture

Systems are small **node networks** (4–6 nodes). Each node contains one encounter — ICE, a barrier (Password), a data cache (File), or a control point (Control Node). Nodes connect to form a map with **routing choices**.

This is a **homebrew departure** from Cyberpunk RED's default linear floor-based Architectures, designed to give the Netrunner meaningful path-selection decisions. RED's mechanical backbone (Interface + 1d10, DVs, Programs, Black ICE stat blocks) is preserved; the structure changes from a straight elevator to a small graph.

### Key Rules

- The Netrunner must be **within 6m** of an access point to Jack In (8m with Range Upgrade). Walls block access.
- **Moving between connected nodes costs 1 NET Action.**
- The Netrunner does **not** see the full map. They see the Gateway and its connections. Entering a node reveals that node's connections.
- **Probe** (free action, see §7) from an adjacent node reveals one fact about the target node: ICE type, DV, or contents (Netrunner's choice).
- **Jacking Out** resets the Architecture's defenses. To make permanent changes, the Netrunner must leave a **Virus** at the Target node.
- If you leave the access point's range while Jacked In, you are **unsafely Jacked Out** (suffer all remaining Rezzed enemy Black ICE effects before exiting).

### Building a System

**Step 1 — Assign Security Rating (SR).**

SR sets the system's scale, base DVs, and ICE composition.

| SR | System Type | Base DV | ICE Present | Alert Threshold |
| :--- | :--- | :---: | :--- | :---: |
| 1 | Personal device, small business | 9–13 | None or Patrol only | 6 |
| 2 | Mid-level corporate, local government | 13–15 | Patrol + Tar | 5 |
| 3 | Major corporate division, military outpost | 15–17 | Patrol + Tar + Black | 4 |
| 4 | Corporate mainframe, intelligence network | 17–21 | All types, multiple instances | 3 |
| 5 | Top-secret military, megacorp core | 21–24 | All types, advanced variants | 3 |

**Step 2 — Draw 4–6 nodes with connections.**

Always include: **1 Gateway** (entry point), **1 Target Node** (what the Netrunner wants), and **2–4 intermediate nodes**. Create at least one **routing choice** (two paths to the Target).

**Step 3 — Populate nodes with encounters.** Each node has one of:

- **ICE** — One of the four behavioral types (see §5). Uses RED Black ICE stat blocks.
- **Barrier (Password)** — Interface (Backdoor) + 1d10 vs DV.
- **Data Cache (File)** — Interface (Eye-Dee) + 1d10 vs DV to identify contents.
- **Control Point (Control Node)** — Interface (Control) + 1d10 vs DV. Disabling may grant a benefit elsewhere (cameras down, doors unlocked, turrets turned on enemies).

**Example — Meridian Regional Office (SR 3):**

```
[Gateway] ─── [Security Hub] ─── [Admin Core]
     │               │
     └── [Data Vault] ┘
            │
     [Research Archive]
```

- **Gateway:** Entry point. Patrol ICE (Wisp) scanning.
- **Security Hub:** Camera/alarm Control Node (DV15). Black ICE (Hellhound) guarding. Disabling this control point gives the Meatspace crew a major advantage (cameras go dark, alarms silenced).
- **Data Vault:** General corporate files (File, DV13). Tar ICE (Skunk) slowing extraction. Connected to both Gateway and Security Hub.
- **Admin Core:** Root access. Password (DV17). Two paths to reach it.
- **Research Archive:** Accessed through Data Vault only. Contains the target data (File, DV17). No ICE, but the highest DV.

**Routing choice:** Go through Security Hub first (fight a Hellhound, but then cameras are disabled and the crew is safer) or skip straight to Data Vault (avoid the hard fight, but Security Hub stays active and Patrol ICE keeps scanning, ticking Alert faster).

### Navigating Nodes

Moving between connected nodes costs **1 NET Action**. The Netrunner describes their approach; the GM resolves the node's encounter. Present the current node's situation and connected nodes the Netrunner can see.

The Netrunner does **not** see the full map. They see the Gateway and its connections. Entering a node reveals its connections. A **Probe** action (free, from an adjacent node) reveals one piece of information about the target node (what type of ICE, the DV, or what data it contains — Netrunner's choice).

---

## 5. ICE (Intrusion Countermeasures Electronics)

ICE occupies nodes and must be dealt with to access the node's contents. Unlike RED's default model where all Black ICE attacks on sight, this system uses **four behavioral categories** that demand different counterplay. Each category uses RED Black ICE stat blocks under the hood.

### ICE Behavioral Types

| ICE Type | Behavior | If Ignored | Recommended Stat Blocks |
| :--- | :--- | :--- | :--- |
| **Patrol** | Scans for intruders. Detection check each round the Netrunner is in the node: `ICE PER + 1d10` vs `Netrunner's Interface + 1d10`. | Raises Alert by 2 on detection. | Wisp, Raven |
| **Tar** | Activates on detection. Netrunner's next NET Action costs +1 Cycle or suffers a −2 penalty to the check. | Stacks. Multiple Tar ICE = multiple penalties. | Skunk, Scorpion |
| **Black** | Attacks on detection. Deals its listed damage effect (brain damage, deck fire, etc.). Full NET combat. **Hunts the Netrunner across nodes** — if the Netrunner moves, Black ICE follows to the new node on its next action. | Attacks every round until Derezzed. The only escape is **Slide** (which pins the ICE at its current node) or Jacking Out. | Hellhound, Giant, Kraken, Liche |
| **Trace** | Begins tracking the Netrunner's physical location on detection. Completes in **(6 − SR) rounds** (minimum 1). | Trace complete = physical location of the access point revealed to the system owner. | Asp, Raven (repurposed) |

**How to assign stat blocks:** Choose the Red Black ICE stat block that fits the narrative threat level. The behavioral category determines *what the ICE does*; the stat block determines *how tough it is*. A Patrol Wisp is a lightweight scanner; a Patrol Raven is a sharper one. A Black Hellhound is a standard attack dog; a Black Kraken is a terrifying trap.

### Dealing with ICE

The Netrunner has four options when facing ICE. Each demands a different trade-off.

| Action | Resolution | Cost | Consequence |
| :--- | :--- | :--- | :--- |
| **Bypass** (stealth) | `Interface + 1d10` vs `ICE PER + 1d10`. Success = pass through the node without triggering the ICE for 1 round. | 1 NET Action | Quiet. ICE remains active. If you return to this node or linger, it may detect you. |
| **Disable** (shutdown) | `Interface + 1d10` vs DV (Base DV + 2). ICE is shut down permanently for this run. | 1 NET Action + 1 Cycle | Permanent and quiet. Costs a Cycle. |
| **Data Spike** (destroy) | `Interface + 1d10` vs `ICE DEF + 1d10`. Success = ICE Derezzed immediately. | 1 NET Action | Free, but loud — Alert +1. ICE is Derezzed (not permanently destroyed unless the check beats the DEF by 5+). |
| **Crash** (program kill) | Expend an **Attacker Program** (Sword, Banhammer, etc.) to auto-Derez one ICE. No check required. | 1 NET Action + 1 Program use | Silent and guaranteed, but burns a Program (Attack Programs Deactivate after use). No Alert increase. |

**NET Combat (Black ICE):** If a Black-type ICE detects the Netrunner and the Netrunner chooses to fight (or has no choice), use RED's standard NET combat:

- **Initiative:** `Interface + SPD bonuses + 1d10` vs `Black ICE SPD + 1d10`. Loser suffers the winner's effect immediately.
- **Attacking ICE:** `Interface + Program ATK + 1d10` vs `ICE DEF + 1d10`. Damage = Program's listed effect.
- **Zap (no Program needed):** `Interface + 1d10` vs `ICE DEF + 1d10`. Deals 1d6 REZ damage.
- **Slide (flee):** `Interface + 1d10` vs `ICE PER + 1d10`. Escape to an adjacent node; **the ICE stays where it was** (does not follow). Once per Turn. Cannot Slide preemptively. This is the **only way** to escape Black ICE without Derezzing it — simply moving to another node does NOT work, as Black ICE hunts across nodes.
- **Black ICE Hunting:** Triggered Black ICE pursues the Netrunner through the Architecture. If the Netrunner moves to a different node (via Enter Node, not Slide), the Black ICE follows on its next action. Update `ice_status` to reflect the ICE's new node. Only Slide, Derezzing, or Jacking Out stops the pursuit.
- A Program or ICE at **0 REZ** is **Derezzed** (inactive). Must be Deactivated and Reactivated (2 NET Actions) to use again.
- Anti-Program Black ICE can **Destroy** Programs permanently (erased from Cyberdeck).

### Black ICE Stat Blocks (RED Core)

**Anti-Personnel** (targets Netrunners)

| Name | PER | SPD | ATK | DEF | REZ | Effect | Cost |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- | :--- |
| **Asp** | 4 | 6 | 2 | 2 | 15 | Destroys a random Program on target's deck. | 100eb |
| **Giant** | 2 | 2 | 8 | 4 | 25 | 3d6 brain damage + forces unsafe Jack Out. | 1,000eb |
| **Hellhound** | 6 | 6 | 6 | 2 | 20 | 2d6 brain damage + ignites Cyberdeck (2 dmg/turn). | 500eb |
| **Kraken** | 6 | 2 | 8 | 4 | 30 | 3d6 brain damage; Netrunner cannot move between nodes. | 1,000eb |
| **Liche** | 8 | 2 | 6 | 2 | 25 | Target INT, REF, DEX lowered by 1d6 for 1 hour. | 500eb |
| **Raven** | 6 | 4 | 4 | 2 | 15 | Derezzes a random Defender program; 1d6 brain damage. | 50eb |
| **Scorpion** | 2 | 6 | 2 | 2 | 15 | Target MOVE lowered by 1d6 for 1 hour. | 100eb |
| **Skunk** | 2 | 4 | 4 | 2 | 10 | Target makes Slide checks at −2 until Derezzed. | 500eb |
| **Wisp** | 4 | 4 | 4 | 2 | 15 | 1d6 brain damage; −1 NET Action next turn. | 50eb |

**Anti-Program** (targets Rezzed Programs)

| Name | PER | SPD | ATK | DEF | REZ | Effect | Cost |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- | :--- |
| **Dragon** | 6 | 4 | 6 | 6 | 30 | 6d6 REZ to Program. If Derezzed, it is **Destroyed**. | 1,000eb |
| **Killer** | 4 | 8 | 6 | 2 | 20 | 4d6 REZ to Program. If Derezzed, it is **Destroyed**. | 500eb |
| **Sabertooth** | 8 | 6 | 6 | 2 | 25 | 6d6 REZ to Program. If Derezzed, it is **Destroyed**. | 1,000eb |

### Demons (SR 4–5 Only)

Demons occupy nodes near Control Nodes. They operate Meatspace defenses (turrets, drones, traps) and cannot be stored in Cyberdecks. Max 1 Demon per 6 nodes.

| Demon | REZ | Interface | NET Actions | Combat Number | Cost |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **Imp** | 15 | 3 | 2 | 14 | 1,000eb |
| **Efreet** | 25 | 4 | 3 | 14 | 5,000eb |
| **Balron** | 30 | 7 | 4 | 14 | 10,000eb |

---

## 6. Alert Level

Homebrew system layered on top of Cyberpunk RED to add tension and escalation to Quick Hacks and Full Runs. Tracked from 0 upward. Represents the system's awareness that an intrusion is occurring.

### Alert Increases

| Event | Alert Increase |
| :--- | :--- |
| Failed Interface check | +1 |
| Patrol ICE detects the Netrunner | +2 |
| Data Spike (destroying ICE) | +1 |
| Brute Force entry to a node | +2 |
| Each round spent in a node after 3 rounds | +1 |

### Alert Thresholds

| Alert Level | System Response |
| :--- | :--- |
| 1–2 | **Elevated.** No mechanical effect. System logs anomaly. |
| 3–4 | **Active Search.** All DVs in the system increase by +2. Patrol ICE rolls detection with a +2 bonus. |
| 5–6 | **Lockdown.** Nodes begin sealing. The Netrunner must pass an Interface check (DV = Base DV) to move between nodes. New Trace ICE activates at the Gateway. |
| 7+ | **Convergence.** System owner is alerted in real-time. Black ICE spawns at the Netrunner's current node. Physical security response dispatched to Netrunner's last known location (if Trace completed) or general area. The Netrunner should get out. |

### Alert Reduction

- **Cloak (Interface Ability):** Before Jacking Out, the Netrunner can use Cloak to erase evidence of the intrusion. This does not reduce Alert mid-run, but determines whether the intrusion is traceable afterward. The DV for another Netrunner to discover the intrusion equals the Cloak check.
- **Mask (Boosted Action, 1 Cycle):** Suppresses Alert increase from the Netrunner's next failed check or loud action this round. See §7.
- **No other reductions during a run.** Alert only resets by Jacking Out entirely. When the Netrunner Jacks Out, the Architecture resets its defenses, but the Alert level's *consequences* (logged anomalies, dispatched security, notified owners) persist in the narrative.

---

## 7. NET Actions & Interface Abilities

These are the Netrunner's verbs during Quick Hacks and Full Runs. **Basic actions** cost 1 NET Action; **Boosted actions** cost 1 NET Action + 1 Cycle.

### NET Actions per Turn

On their Turn, a Netrunner chooses to either take **1 Meat Action** (shoot, move, etc.) OR use **NET Actions** in the Architecture. The number of NET Actions depends on **Interface Rank**.

| Interface Rank | 1–3 | 4–6 | 7–9 | 10 |
| :--- | :---: | :---: | :---: | :---: |
| **NET Actions** | 2 | 3 | 4 | 5 |

### Basic Actions (1 NET Action Each)

| Action | Effect |
| :--- | :--- |
| **Probe** | **Free action** (costs 0 NET Actions). From an adjacent node, learn one fact about the target node: ICE type, DV, or contents (Netrunner's choice). |
| **Enter Node** | Move to a connected node. Triggers any ICE encounter in the destination (Patrol scans, Tar activates, Black attacks, Trace begins). |
| **Backdoor Entry** | Enter a node stealthily. `Interface + 1d10` vs `highest ICE PER in node + 1d10`. Success = enter without triggering ICE for 1 round. |
| **Brute Force** | Enter a node and immediately overcome its barrier (Password). Auto-success on the Backdoor check. Alert +2. |
| **Data Spike** | Attack one ICE. `Interface + 1d10` vs `ICE DEF + 1d10`. Success = ICE Derezzed. Alert +1. |
| **Jack In** | Enter an Architecture within 6m (8m with Range Upgrade). |
| **Jack Out** | Disconnect safely. Resets Architecture defenses. Unsafe if Trace ICE has completed (physical location already revealed). |
| **Interact** | Access a data cache (File), activate a control point (Control Node), or download files in the current node. May require an Interface check (Eye-Dee, Control) vs the node's DV. |
| **Activate / Deactivate Program** | Ready or stow a Program from your Cyberdeck. |
| **Use Interface Ability** | Use Backdoor, Cloak, Control, Eye-Dee, Pathfinder, Slide, Virus, or Zap. See below. |

### Boosted Actions (1 NET Action + 1 Cycle Each)

| Action | Effect |
| :--- | :--- |
| **Surge** | Add +4 to your next Interface check this round. |
| **Mask** | Suppress the Alert increase from your next failed check or loud action this round. |
| **Overclock** | Take two basic actions with your next NET Action this round (effectively gaining 1 extra action). |
| **Fortify** | Until your next turn, reduce all brain damage you take by 4 (stacks with Armor program). |
| **Spoof Signal** | Create a false signature at a node you've previously visited. Patrol ICE in your current node investigates the spoofed location instead of scanning you. Lasts 2 rounds. |

### Interface Abilities

All Interface checks: `Interface + 1d10 (+ any active Booster bonus)` vs the listed DV or an opposed roll.

| Ability | Effect |
| :--- | :--- |
| **Scanner** | **Meat Action** (not a NET Action). Locate access points to NET Architectures in the area. The higher the check, the more you find and the farther you detect. |
| **Backdoor** | Break through a Password. `Interface + 1d10` vs the Password's DV. If you already know the password, auto-pass. |
| **Cloak** | Hide evidence of your presence and any Viruses you left. The DV for another Netrunner to discover your actions (via Pathfinder) equals your Cloak check. Once per run. |
| **Control** | Seize a Control Node. `Interface + 1d10` vs the Node's DV. Operating each device attached to the node costs a separate NET Action. Usable from anywhere in the Architecture. One activation per Node per Turn. Lost on Jack Out. |
| **Eye-Dee** | Identify the contents of a File. `Interface + 1d10` vs the File's DV. |
| **Pathfinder** | Reveal the Architecture's layout. Reveals a number of nodes equal to your check result divided by 4 (round down, minimum 1), or until an obstruction with a DV higher than your check. Once per run. |
| **Slide** | Flee combat with a single non-Demon Black ICE. `Interface + 1d10` vs `ICE PER + 1d10`. Escape to an adjacent node; **the ICE stays where it was** (does not follow). Once per Turn. Cannot Slide preemptively. This is the only way to escape hunting Black ICE without Derezzing it — Enter Node does not work because Black ICE pursues. |
| **Virus** | Available only at the **Target Node**. Leave a Virus to perform up to 2 persistent changes to the Architecture (the only way to make changes that last after Jack Out). GM assigns DV. The DV to destroy your Virus equals your Virus check. |
| **Zap** | Basic attack (no Program needed). `Interface + 1d10` vs `target DEF + 1d10` (or enemy Netrunner's `Interface + 1d10`). Deals **1d6** damage to Program REZ or directly to a Netrunner's brain. |

### Program Usage in the Architecture

Any active Program can be used during a run at its normal cost. **Attacker Programs** are particularly useful for the **Crash** action (auto-Derez one ICE, no check, no Alert increase). Other Programs can be used creatively at GM discretion. Some examples:

| Program | Creative Architecture Use |
| :--- | :--- |
| **Sword / Banhammer** | **Crash:** Auto-Derez one ICE. No check, no Alert increase. Consumes the Program (Deactivates after use). |
| **DeckKRASH** | Force an enemy Netrunner out of the Architecture. |
| **Nervescrub** | Weaken an enemy Netrunner's stats, making their Interface checks worse. |
| **Superglue** | Pin an enemy Netrunner in place while you work. |
| **Eraser + Cloak** | Combined: erase your presence with a +2 bonus. Nearly untraceable. |
| **See Ya + Pathfinder** | Combined: map the Architecture with a +2 bonus. Better routing intel. |

### NET Combat

When attacking with a Program: `Interface + Program ATK + 1d10` vs `Target DEF + 1d10`.

- **Brain damage** is applied directly to HP. It ignores worn/implanted armor and cannot cause Critical Injuries.
- **Attack Programs** Deactivate themselves after use. Programs cannot be used more than once per round.
- A Program at **0 REZ** is Derezzed. It stays on the deck but is unusable until Deactivated and Reactivated (2 NET Actions total).
- A **Destroyed** Program is permanently erased from the Cyberdeck.

---

## 8. Cyberdeck Upgrades

Cyberdecks can be replaced by purchasing a higher-quality model. Hardware and Programs can be swapped freely (1 hour per install/uninstall).

Decks are **not cyberware**. No Humanity cost, no implant slot required. They are external equipment — worn on belt, wrist-mounted, or carried in a bag.

When upgrading, consider prioritizing **slots** and **Cycles** for your playstyle:

- **Combat Netrunner:** Sword, Banhammer, Armor, Shield, a Black ICE (2 slots) = 6 slots minimum. Needs Standard or Excellent deck. Cycles mostly spent on Fortify and Overclock.
- **Stealth Netrunner:** Eraser, See Ya, Worm, Armor, Shield = 5 slots. A Poor deck works at low levels. Cycles spent on Mask and Spoof Signal.
- **Support Netrunner:** Worm, See Ya, Flak, Shield, Speedy Gonzalvez = 5 slots. Add Hardware (KRASH Barrier, Insulated Wiring) with an Excellent deck. Cycles spent on Surge and Overclock to grab Control Nodes fast.

---

## 9. GM Quick Reference

### Running a Full Run — Checklist

1. Determine SR based on the target system.
2. Draw 4–6 nodes with connections. Place ICE, barriers, data caches, and control points. Ensure at least one routing choice.
3. Describe the Gateway node and its visible connections.
4. Set Alert to 0.
5. Roll initiative with the full group. Each round: the Netrunner declares their NET Actions → resolve each → update Alert → describe the result and new options. Other players take Meat Actions on their own initiative counts.
6. When the Netrunner reaches the Target Node and completes their objective (or Jacks Out), end the run.
7. Summarize outcome: what was obtained, final Alert level, any consequences (Trace completion, physical security response, logged intrusions, etc.).

### When NOT to Use Full Runs

- The hack is a means to an end, not the scene itself. → **Use Simple Check.**
- The player is eager to advance the story. → **Use Quick Hack at most.**
- The hack doesn't involve meaningful risk or interesting choices. → **Use Simple Check.**
- You've already run a Full Run this session or last session. → **Use Quick Hack.**

### Node Design Principles

- **Always offer a routing choice.** Two paths to the Target: one harder but more rewarding (e.g., disable security for the Meatspace crew), one easier but with compounding consequences (e.g., Patrol ICE keeps scanning, ticking Alert).
- **Mix ICE types.** Don't put only Black ICE in every node. Patrol ICE creates tension; Tar ICE creates resource pressure; Trace ICE creates urgency; Black ICE creates danger. A good 5-node system might have: 1 Patrol, 1 Tar, 1 Black, 1 Password, 1 File.
- **Connect nodes to the Meatspace scene.** Control Nodes should do things the rest of the crew cares about: disable cameras, unlock doors, activate turrets against guards, cut the lights. This keeps the Netrunner's choices relevant to everyone at the table.
- **Keep it to 4–6 nodes.** More than 6 and the run drags. Fewer than 4 and routing choices disappear.

### Keeping Netrunning Concurrent

Cyberpunk RED's Netrunning is designed to happen **during regular combat initiative**, not as a separate scene. The Netrunner rolls initiative with everyone else. On their turn, they choose Meat Action or NET Actions — not both.

While the Netrunner descends through nodes, the rest of the crew should be active in Meatspace: fighting guards, looting, holding position, running distraction. The Netrunner's Control Node access (disabling cameras, unlocking doors, activating turrets against enemies) directly helps the crew — this is where the role shines.

### Pacing NET Actions

A Netrunner with Interface 4–6 gets 3 NET Actions per turn. A typical 5-node system might take 4–6 rounds to clear, depending on obstacles and routing. That's roughly the same as a mid-length firefight. If it's dragging:

- The Netrunner doesn't have to fight everything. **Bypass** and **Slide** let them move through fast.
- **Probe** is free — encourage the Netrunner to scout ahead before committing to a path.
- If the crew's Meatspace scene resolves first, compress remaining nodes narratively or let the Netrunner describe a fast exit.
- Conversely, if the Netrunner finishes fast, have the consequences of their run ripple into Meatspace (alarms going off, doors unlocking, turrets activating).

### Narrating the NET

Describe the NET as the Netrunner experiences it through Virtuality — not literal rooms, but as an abstract digital landscape layered over reality. A corporate security network might feel cold and geometric. A personal device might feel cluttered and warm. A military system might feel like pressure and weight. A megacorp core system might feel alive.

Use sensory language: data streams as light, ICE as presence or resistance, Passwords as density or cold. The Netrunner doesn't walk through corridors — they push through layers of information, with the real world still visible beneath the Virtuality overlay.

### Quick DV Reference

| Difficulty | DV | Example |
| :--- | :---: | :--- |
| Simple | 9 | Unlocked personal device, public terminal. |
| Everyday | 13 | Encrypted personal comms, small business system. |
| Difficult | 15 | Corporate employee terminal, secured government files. |
| Professional | 17 | Corporate mainframe access, military-grade encryption. |
| Heroic | 21 | Intelligence network core, megacorp black project. |
| Incredible | 24 | Top-secret military AI, prototype NET defenses. |
| Legendary | 29 | Arasaka/Militech inner sanctum, relic-tier data. |

### Sample Architectures

**Personal Device (SR 1, 3 Nodes)**
```
[Gateway] ─── [Storage]
     │
[Comms Log]
```
- Gateway: Empty (no ICE). Just jack in.
- Storage: File (DV9). Personal photos, contacts, messages.
- Comms Log: File (DV13). Encrypted messages. Slightly harder but more valuable.

**Maelstrom Den (SR 2, 4 Nodes)**
```
[Gateway] ─── [Sec Cameras] ─── [Armory Access]
     │
[Crew Roster]
```
- Gateway: Patrol ICE (Wisp). Scanning for intruders.
- Sec Cameras: Control Node (DV13). Disabling gives the Meatspace crew free movement.
- Armory Access: Control Node (DV15). Unlock the armory door remotely. Tar ICE (Skunk) slowing access.
- Crew Roster: File (DV13). Names, addresses, augmentation records.
- **Routing choice:** Go for the cameras first (safer for the crew, but costs time while Patrol ticks Alert) or rush the Armory (faster, but the crew is still on cameras).

**Arasaka Branch Office (SR 4, 6 Nodes)**
```
[Gateway] ─── [Firewall Hub] ─── [Executive Files]
     │               │
     └── [Personnel] ─┤
            │         │
     [Server Farm] ── [Ops Center]
```
- Gateway: Patrol ICE (Raven). High PER, good at spotting intruders.
- Firewall Hub: Black ICE (Hellhound). The hard-but-rewarding path — clearing it makes everything easier.
- Personnel: Tar ICE (Scorpion). Slows operations. File (DV17) with employee records.
- Server Farm: Trace ICE (Asp, repurposed). Starts tracking physical location. File (DV17) with financial data.
- Executive Files: Password (DV21). The target. No ICE, but brutal DV.
- Ops Center: Control Node (DV21). Efreet Demon. Controls building turrets and doors. The nuclear option for the Meatspace crew.
- **Routing choices:** Three possible paths to Executive Files. Through Firewall Hub (hard fight, but a clear road after). Through Personnel → Server Farm (avoid the fight, but Tar slows you and Trace is ticking). Or the long way through Server Farm → Ops Center (grab the Demon's toys, but that's an SR 4 Demon fight).
