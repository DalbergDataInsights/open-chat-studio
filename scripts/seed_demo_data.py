"""Seed a local "Demo Team" for recording the DDI feature walkthrough. Local dev databases only.

Usage: uv run python scripts/seed_demo_data.py [--reset] [--email EMAIL] [--password PASSWORD]
"""

import argparse
import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.channels.models import ChannelPlatform, ExperimentChannel  # noqa: E402
from apps.chat.models import Chat, ChatMessage, ChatMessageType  # noqa: E402
from apps.dashboard.models import DashboardCache  # noqa: E402
from apps.experiments.models import (  # noqa: E402
    Experiment,
    ExperimentSession,
    Participant,
    SessionStatus,
    SourceMaterial,
)
from apps.pipelines.models import Pipeline  # noqa: E402
from apps.service_providers.models import LlmProvider, LlmProviderModel  # noqa: E402
from apps.teams.backends import get_team_owner_groups  # noqa: E402
from apps.teams.models import Flag, Membership, Team  # noqa: E402
from apps.teams.utils import set_current_team  # noqa: E402

TEAM_SLUG = "demo-team"
RNG = random.Random(2927)

QUESTIONS = [
    "What time does the clinic open?",
    "Is it safe to take paracetamol while pregnant?",
    "When should I plant maize this season?",
    "How do I treat leaf rust on my beans?",
    "What vaccines does my baby need at 6 weeks?",
    "Can I get a reminder for my next appointment?",
    "What are the signs of malaria?",
    "How much fertiliser should I use per acre?",
]
ANSWERS = [
    "Thanks for asking. Here is what the guidelines say.",
    "Good question. The short answer is yes, with a few precautions.",
    "Based on your area, the best window is the next two weeks.",
    "I've noted that. Is there anything else I can help with?",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Delete the existing demo team first")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", default="demo-password-2026")
    args = parser.parse_args()

    existing = Team.objects.filter(slug=TEAM_SLUG).first()
    if existing:
        if not args.reset:
            sys.exit(f"Team '{TEAM_SLUG}' already exists. Re-run with --reset to rebuild it.")
        print("Deleting the existing demo team...")
        existing.delete()

    with transaction.atomic():
        team, user = _team_and_user(args.email, args.password)
        provider, provider_model = _llm_provider(team)
        bots = _chatbots(team, user, provider, provider_model)
        _source_material_scenarios(team, user, bots)
        _unarchive_scenarios(team, user, provider, provider_model, bots)
        _engagement_history(team, bots)
        _enable_engagement_flag(team)
        DashboardCache.objects.filter(team=team).delete()

    print(f"\nDone. Log in as {args.email} / {args.password} and open the team '{team.name}'.")


def _team_and_user(email, password):
    team = Team.objects.create(name="Demo Team", slug=TEAM_SLUG)
    set_current_team(team)
    user_model = get_user_model()
    user = user_model.objects.filter(email=email).first()
    if not user:
        user = user_model.objects.create_superuser(username=email, email=email, password=password)
        print(f"Created user {email}")
    membership = Membership.objects.create(team=team, user=user)
    membership.groups.add(*get_team_owner_groups())
    return team, user


def _llm_provider(team):
    provider = LlmProvider.objects.create(
        team=team, type="openai", name="OpenAI (demo key)", config={"openai_api_key": "sk-demo-not-a-real-key"}
    )
    provider_model = LlmProviderModel.objects.filter(team=None, type="openai", name="gpt-5.4-mini").first()
    if not provider_model:
        provider_model = LlmProviderModel.objects.create(team=team, type="openai", name="gpt-4o-mini")
    return provider, provider_model


def _new_bot(team, user, provider, provider_model, name, description):
    pipeline = Pipeline.create_default(
        team, name=f"{name} pipeline", llm_provider_id=provider.id, llm_provider_model=provider_model
    )
    return Experiment.objects.create(team=team, owner=user, name=name, description=description, pipeline=pipeline)


def _llm_node(bot):
    return bot.pipeline.node_set.get(type="LLMResponseWithPrompt")


def _use_source_material(bot, source_material):
    node = _llm_node(bot)
    params = dict(node.params)
    params["source_material_id"] = source_material.id
    params["prompt"] = "You are a helpful assistant. Use this reference material:\n{source_material}"
    node.set_params(params)


def _channel(team, bot, platform, name, extra_data):
    return ExperimentChannel.objects.create(
        team=team, experiment=bot, platform=platform, name=name, extra_data=extra_data
    )


def _chatbots(team, user, provider, provider_model):
    maternal = _new_bot(
        team,
        user,
        provider,
        provider_model,
        "Maternal Health Assistant",
        "Answers pregnancy and newborn care questions",
    )
    farmer = _new_bot(
        team, user, provider, provider_model, "Farmer Advisory Bot", "Planting, pests and fertiliser advice"
    )
    clinic = _new_bot(
        team, user, provider, provider_model, "Clinic FAQ Bot", "Opening hours, services and appointments"
    )
    events = _new_bot(
        team, user, provider, provider_model, "Event Reminder Bot", "Sends reminders for community health events"
    )
    events.create_new_version(version_description="First release")

    web = ExperimentChannel.objects.get_team_web_channel(team)
    api = ExperimentChannel.objects.get_team_api_channel(team)
    channels = {
        "maternal_whatsapp": _channel(
            team, maternal, ChannelPlatform.WHATSAPP, "Maternal WhatsApp", {"number": "+15550001001"}
        ),
        "farmer_telegram": _channel(
            team, farmer, ChannelPlatform.TELEGRAM, "Farmer Telegram", {"bot_token": "demo-farmer-token"}
        ),
        "events_telegram": _channel(
            team, events, ChannelPlatform.TELEGRAM, "Events Telegram", {"bot_token": "demo-events-token"}
        ),
    }
    print("Created chatbots: Maternal Health Assistant, Farmer Advisory Bot, Clinic FAQ Bot, Event Reminder Bot")
    return {
        "maternal": maternal,
        "farmer": farmer,
        "clinic": clinic,
        "events": events,
        "web": web,
        "api": api,
        **channels,
    }


def _source_material_scenarios(team, user, bots):
    clinic_hours = SourceMaterial.objects.create(
        team=team,
        owner=user,
        topic="Clinic opening hours",
        description="Hours and services for the clinic FAQ bot",
        material="Mon-Fri 08:00-17:00, Sat 09:00-13:00. Closed on public holidays.",
    )
    _use_source_material(bots["clinic"], clinic_hours)

    SourceMaterial.objects.create(
        team=team,
        owner=user,
        topic="Old onboarding script",
        description="Not used by any chatbot",
        material="Welcome to the pilot. This script is no longer used.",
    )

    guidelines = SourceMaterial.objects.create(
        team=team,
        owner=user,
        topic="Health guidelines",
        description="Shared by two chatbots",
        material="Version 1 of the community health guidelines.",
    )
    _use_source_material(bots["maternal"], guidelines)
    _use_source_material(bots["farmer"], guidelines)
    # Both v1 chatbot versions share one Source Material version; the edit makes the v2s mint a new one.
    bots["maternal"].create_new_version(version_description="First release")
    bots["farmer"].create_new_version(version_description="First release")
    guidelines.material = "Version 2 of the community health guidelines, updated in September."
    guidelines.save()
    bots["maternal"].create_new_version(version_description="Updated guidelines", make_default=True)
    bots["farmer"].create_new_version(version_description="Updated guidelines", make_default=True)
    print(
        "Created Source Material: Clinic opening hours (in use), Old onboarding script (unused), "
        "Health guidelines (shared)"
    )


def _unarchive_scenarios(team, user, provider, provider_model, bots):
    pilot = _new_bot(
        team, user, provider, provider_model, "Pilot Bot", "The 2026 pilot chatbot, archived after the pilot"
    )
    pilot.create_new_version(version_description="Pilot release")
    _channel(team, pilot, ChannelPlatform.TELEGRAM, "Pilot Telegram", {"bot_token": "demo-pilot-token"})
    _channel(team, pilot, ChannelPlatform.WHATSAPP, "Pilot WhatsApp", {"number": "+15550009999"})
    bots["pilot"] = pilot
    bots["pilot_telegram"] = pilot.experimentchannel_set.get(platform=ChannelPlatform.TELEGRAM)
    bots["pilot_whatsapp"] = pilot.experimentchannel_set.get(platform=ChannelPlatform.WHATSAPP)
    # Activity is generated before archiving below, so keep a handle on the live channels.
    bots["_archive_after_history"] = pilot

    successor = _new_bot(team, user, provider, provider_model, "Pilot Bot v2", "Took over the pilot's Telegram bot")
    bots["successor"] = successor
    print("Created Pilot Bot (archived after history is generated) and Pilot Bot v2")


def _engagement_history(team, bots):
    now = timezone.now()
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    for _ in range(6):
        start = (start - timedelta(days=1)).replace(day=1)

    pilot_end = start + timedelta(days=110)
    streams = [
        # (chatbot, channel, platform, share of new participants, last date new participants join)
        (bots["maternal"], bots["maternal_whatsapp"], ChannelPlatform.WHATSAPP, 0.30, now),
        (bots["maternal"], bots["web"], ChannelPlatform.WEB, 0.10, now),
        (bots["farmer"], bots["farmer_telegram"], ChannelPlatform.TELEGRAM, 0.25, now),
        (bots["clinic"], bots["web"], ChannelPlatform.WEB, 0.15, now),
        (bots["clinic"], bots["api"], ChannelPlatform.API, 0.05, now),
        (bots["events"], bots["events_telegram"], ChannelPlatform.TELEGRAM, 0.05, now),
        (bots["pilot"], bots["pilot_telegram"], ChannelPlatform.TELEGRAM, 0.10, pilot_end),
    ]

    participants, sessions_plan = [], []
    week = start
    week_index = 0
    while week < now:
        new_this_week = int(12 + week_index * 1.6 + RNG.randint(-3, 3))
        for _ in range(new_this_week):
            bot, channel, platform, _share, last_join = RNG.choices(streams, weights=[s[3] for s in streams])[0]
            joined = week + timedelta(seconds=RNG.randint(0, 7 * 86400 - 1))
            if joined >= min(now, last_join):
                continue
            profile = RNG.choices(["core", "regular", "one_off"], weights=[0.3, 0.3, 0.4])[0]
            participant = Participant(
                team=team,
                platform=platform,
                identifier=_identifier(platform, len(participants)),
                name=f"Participant {len(participants) + 1}",
            )
            participants.append((participant, joined))
            sessions_plan.extend(
                (participant, bot, channel, platform, when)
                for when in _session_times(joined, profile, min(now, last_join + timedelta(days=30)))
            )
        week += timedelta(days=7)
        week_index += 1

    Participant.objects.bulk_create([p for p, _ in participants])
    for participant, joined in participants:
        participant.created_at = joined
    Participant.objects.bulk_update([p for p, _ in participants], ["created_at"], batch_size=1000)

    chats = Chat.objects.bulk_create([Chat(team=team, name="Demo chat") for _ in sessions_plan], batch_size=1000)
    sessions, messages = [], []
    for chat, (participant, bot, channel, platform, when) in zip(chats, sessions_plan, strict=True):
        turns = RNG.randint(2, 6)
        clock = when
        for _ in range(turns):
            messages.append(
                (ChatMessage(chat=chat, message_type=ChatMessageType.HUMAN, content=RNG.choice(QUESTIONS)), clock)
            )
            clock += timedelta(seconds=RNG.randint(20, 90))
            messages.append(
                (ChatMessage(chat=chat, message_type=ChatMessageType.AI, content=RNG.choice(ANSWERS)), clock)
            )
            clock += timedelta(seconds=RNG.randint(40, 240))
        is_recent = now - clock < timedelta(days=1)
        sessions.append(
            (
                ExperimentSession(
                    team=team,
                    experiment=bot,
                    participant=participant,
                    chat=chat,
                    experiment_channel=channel,
                    platform=platform,
                    status=SessionStatus.ACTIVE if is_recent else SessionStatus.COMPLETE,
                    ended_at=None if is_recent else clock,
                ),
                when,
            )
        )

    ExperimentSession.objects.bulk_create([s for s, _ in sessions], batch_size=1000)
    for session, when in sessions:
        session.created_at = when
    ExperimentSession.objects.bulk_update([s for s, _ in sessions], ["created_at"], batch_size=1000)

    ChatMessage.objects.bulk_create([m for m, _ in messages], batch_size=2000)
    for message, when in messages:
        message.created_at = when
    ChatMessage.objects.bulk_update([m for m, _ in messages], ["created_at"], batch_size=2000)
    Chat.objects.filter(id__in=[c.id for c in chats]).update(created_at=start)

    pilot = bots.pop("_archive_after_history")
    pilot.archive()
    _channel(
        pilot.team,
        bots["successor"],
        ChannelPlatform.TELEGRAM,
        "Pilot Telegram (v2)",
        {"bot_token": "demo-pilot-token"},
    )
    print(
        f"Created {len(participants)} participants, {len(sessions)} sessions, {len(messages)} messages "
        f"since {start:%d %b %Y}"
    )
    print("Archived Pilot Bot; Pilot Bot v2 now holds its Telegram token")


def _session_times(joined, profile, last_active):
    times = [joined]
    weekly_chance = {"core": 0.8, "regular": 0.35, "one_off": 0.04}[profile]
    churn = {"core": 0.03, "regular": 0.08, "one_off": 0.5}[profile]
    week_start = joined + timedelta(days=7)
    while week_start < last_active:
        if RNG.random() < churn:
            break
        if RNG.random() < weekly_chance:
            for _ in range(RNG.choice([1, 1, 2])):
                when = week_start + timedelta(seconds=RNG.randint(0, 7 * 86400 - 1))
                if when < last_active:
                    times.append(when)
        week_start += timedelta(days=7)
    return times


def _identifier(platform, index):
    match platform:
        case ChannelPlatform.WHATSAPP:
            return f"+2547{10000000 + index}"
        case ChannelPlatform.TELEGRAM:
            return str(500000000 + index)
        case _:
            return f"participant{index}@example.com"


def _enable_engagement_flag(team):
    flag, _ = Flag.objects.get_or_create(name="flag_engagement_dashboard")
    flag.teams.add(team)
    flag.flush()
    print("Turned on flag_engagement_dashboard for the demo team")


if __name__ == "__main__":
    main()
