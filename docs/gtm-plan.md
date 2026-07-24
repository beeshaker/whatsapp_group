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

## Phase 2 — WhatsApp sales bot (in progress, built 2026-07-24)

The site's "Chat on WhatsApp" button opens a DM with the ops/demo number; unknown DMs there now get answered by an LLM sales agent (`answer_sales_query` in `backend/chat.py`), gated behind a `SALES_DM_MODE` flag that must stay off on every client deployment. Built on the existing OpenWA/Baileys engine as a near-term stopgap, not a long-term platform choice — see deployment steps and rationale in `docs/vps-architecture.md`'s "Ops-gateway (sales bot) deployment" section.

## Phase 3 roadmap (not built yet)

- **Migrate the WhatsApp sales bot to Meta's official WhatsApp Business Cloud API**, as its own separate project — decided 2026-07-24. The current OpenWA/Baileys-based `SALES_DM_MODE` wiring is intentionally a stopgap to get the site's WhatsApp CTA working now; it is not meant to be the long-term implementation.
- **ElevenLabs conversational voice agent for both channels** — a voice widget on the marketing website, and a voice channel on WhatsApp — fed by the same knowledge base as the sales bot, for prospects who'd rather talk than type.
- **Trigger to revisit:** once the site + referral/outreach motion is producing enough conversations that the current stopgap's limits (rate limits/ban risk inherent to unofficial WhatsApp libraries, no voice support) start to bite.
