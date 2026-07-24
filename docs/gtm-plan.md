# Whats2Manage — Go-To-Market Plan

Last updated: 2026-07-24

## Ideal customer profile

**Primary:** Nairobi property/facility management companies managing 3+ buildings or estates, with 3–15 staff, currently coordinating maintenance requests via informal WhatsApp groups.

**Secondary:** Real estate brokerages (lead capture use case), fleet/delivery operators (vehicle-issue ticketing use case).

## Positioning

**"The ticketing system your team never has to learn — because it's already inside WhatsApp."**

Messaging pillars:
1. Zero behavior change for tenants/staff — no new app, no training.
2. Nothing falls through the cracks — every reported issue becomes a tracked ticket with a deadline.
3. Built-in accountability — reaction-based status updates, full history, daily summaries.
4. Replaces WhatsApp chaos for less than the cost of the chaos (missed maintenance, lost leads, unhappy tenants).

## 90-day goal

**8–10 new paying clients by 2026-10-22.**

## Channels & weekly cadence (solo-run, bootstrap budget)

Ranked by expected leverage given the team is one person and the proven channel so far is referral.

1. **Referral engine — start week 1.**
   Message all 4 existing clients (Dunhill, Pixie, Nineonetwo, Pixiilive) asking for 2–3 warm introductions each. Offer one month free per successful signed referral. Target: 12–16 warm intros from this alone.

2. **Direct outreach.**
   Build a target list of 60–80 Nairobi property management companies (Google Maps, LinkedIn, KPDA/EARB member directories). Send 15–20 personalized WhatsApp/LinkedIn messages per week, each pointing to a short demo video and the marketing site. Target: 3–4 demo conversations per week.

3. **Content/SEO.**
   Publish one short landing/blog piece every 2 weeks targeting Kenya-specific search terms: "property management software Kenya," "WhatsApp ticketing system," "maintenance request tracking Nairobi." Hosted on the marketing site — cheap and compounds over time.

4. **Community presence.**
   Participate (non-spammy, value-first) in 3–5 relevant Facebook/WhatsApp/LinkedIn groups for Kenyan property managers and real estate professionals.

5. **Local associations.**
   Evaluate KPDA / EARB membership or event sponsorship after month 1, once channels 1–2 are producing signal.

## Sales / demo motion

Site or outreach → WhatsApp chat → qualify (number of buildings/groups, current pain) → live dashboard demo (screen share or recorded Loom) → free Tier-1 trial → convert to paid via M-Pesa.

## Funnel metrics to track

Keep a simple spreadsheet:
- Site visits → WhatsApp chats opened → demos booked → trials started → paid conversions
- Referral asks sent → referrals received → converted

## Phase 2 roadmap (not built yet)

- **WhatsApp knowledge-base bot** on the existing ops/demo number. Reuses this repo's own Baileys/OpenWA engine (`backend/`) and the Ollama LLM already used for ticket classification, fed by a small FAQ/knowledge base (site copy + common objections). Answers prospect DMs on WhatsApp and powers a website chat widget.
- **Optional ElevenLabs conversational voice agent**, fed by the same knowledge base, for prospects who'd rather talk than type.
- **Trigger to revisit:** once the site + referral/outreach motion is producing enough WhatsApp conversations that answering them manually becomes the bottleneck.
