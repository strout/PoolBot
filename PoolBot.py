from __future__ import print_function
# bot.py
import os

import discord
import re
import random
import time
from dotenv import load_dotenv
from typing import Optional, Sequence, Union, List, TypedDict, Tuple
from datetime import datetime
from collections import Counter, defaultdict

import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import aiohttp
import utils

load_dotenv()

SEALEDDECK_URL = "https://sealeddeck.tech/api/pools"

class SealedDeckEntry(TypedDict):
    name: str
    count: int

def arena_to_json(arena_list: str) -> Sequence[SealedDeckEntry]:
    """Convert a list of cards in arena format to a list of json cards"""
    json_list: List[SealedDeckEntry] = []
    for line in arena_list.rstrip("\n ").split("\n"):
        count, card = line.split(" ", 1)
        card_name = card.split(" (")[0]
        json_list.append({"name": f"{card_name}", "count": int(count)})
    return json_list

def remove_cards(pool: Sequence[SealedDeckEntry], cards_to_remove: Sequence[SealedDeckEntry]) -> Sequence[SealedDeckEntry]:
    """Remove the given cards from the pool, decrementing counts or totally removing entries."""
    counted: Counter[str] = Counter()
    for card in pool:
        counted[card["name"]] += card["count"]
    for card in cards_to_remove:
        counted[card["name"]] -= card["count"]
    return [{"name": name, "count": count} for name, count in counted.items() if count > 0]

async def sealeddeck_pool(pool_sealeddeck_id: str) -> Optional[Sequence[SealedDeckEntry]]:
    resp_json = None

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{SEALEDDECK_URL}/{pool_sealeddeck_id}") as resp:
                    resp.raise_for_status()
                    resp_json = await resp.json()
        except:
            continue
        else:
            break

    if resp_json is not None:
        return [*resp_json["sideboard"], *resp_json["deck"], *resp_json["hidden"]]
    else:
        return None

async def pool_to_sealeddeck(
        punishment_cards: Sequence[SealedDeckEntry], pool_sealeddeck_id: Optional[str] = None
) -> str:
    """Adds punishment cards to a sealeddeck.tech pool and returns the id"""
    deck: dict[str, Union[Sequence[dict], str]] = {"sideboard": punishment_cards}
    if pool_sealeddeck_id:
        deck["poolId"] = pool_sealeddeck_id

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(SEALEDDECK_URL, json=deck) as resp:
                    resp.raise_for_status()
                    resp_json = await resp.json()
        except:
            continue
        else:
            break

    return resp_json["poolId"]


async def update_message(message: discord.Message, new_content: str):
    """Updates the text contents of a sent bot message"""
    return await message.edit(content=new_content)


async def message_member(member: Union[discord.Member, discord.User], message: str):
    try:
        await member.send(message)
        # await member.send(
        #     "Greetings, current or former Arena Gauntlet League player! This is your last chance to join us for the Wilds of Eldraine league before registration closes on Wednesday, September 6th at 5pm EST.\n\nSign up here: https://docs.google.com/forms/d/e/1FAIpQLSe44aHmif2QsplYoxdyKDmrpj6hRhywdPLQD4SYhOvhvjfsGA/viewform.\n\nWe hope to see you there!")
        time.sleep(0.25)
    except discord.errors.Forbidden as e:
        print(e)


class PoolBot(discord.Client):
    def __init__(self, config: utils.Config, intents: discord.Intents, *args, **kwargs):
        self.booster_tutor = None
        self.spreadsheet_id = None
        self.awaiting_boosters_for_user = None
        self.num_boosters_awaiting = None
        self.active_lfm_message = None
        self.active_duel_message = None
        self.league_committee_channel = None
        self.bot_bunker_channel = None
        self.lfm_channel = None
        self.duel_channel = None
        self.packs_channel = None
        self.pool_channel = None
        self.side_quest_pools_channel = None
        self.dev_mode = None
        self.pools_tab_id = None
        self.pending_lfm_user_mention = None
        self.pending_duel_user_mention = None
        self.config = config
        self.league_start = datetime.fromisoformat('2022-06-22')
        self.double_packs: dict[int, Sequence[SealedDeckEntry]] = dict()
        super().__init__(intents=intents, *args, **kwargs)

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        await self.user.edit(username='AGL Bot')
        # If this is true, posts will be limited to #bot-lab and #bot-bunker, and LFM DMs will be ignored.
        self.dev_mode = self.config.debug_mode == "active"
        self.pools_tab_id = self.config.pools_tab_id
        self.pool_channel = self.get_channel(719933932690472970) if not self.dev_mode else self.get_channel(
            1065100936445448232)
        self.packs_channel = self.get_channel(798002275452846111) if not self.dev_mode else self.get_channel(
            1065101003168436295)
        self.lfm_channel = self.get_channel(720338190300348559) if not self.dev_mode else self.get_channel(
            1065101040770363442)
        self.bot_bunker_channel = self.get_channel(1000465465572864141) if not self.dev_mode else self.get_channel(
            1065101076002508800)

        self.league_committee_channel = self.get_channel(1052324453188632696) if not self.dev_mode else self.get_channel(
            1065101182525259866)
        self.side_quest_pools_channel = self.get_channel(1055515435073806387)
        self.duel_channel = self.get_channel(1206645629250445342) if not self.dev_mode else self.bot_bunker_channel
        self.pending_lfm_user_mention = None
        self.pending_lfm_user_id = None
        self.active_lfm_message = None
        self.pending_duel_user_mention = None
        self.pending_duel_user_id = None
        self.active_duel_message = None
        self.num_boosters_awaiting = 0
        self.awaiting_boosters_for_user = None
        self.spreadsheet_id = self.config.spreadsheet_id
        for user in self.users:
            if user.name == 'Booster Tutor':
                self.booster_tutor = user
        #
        # for member in self.guilds[0].members:
        #     if member.bot:
        #         continue
        #     for role in member.roles:
        #         if 'Lord of the Rings' in role.name:
        #             # print(member.display_name)
        #             await self.packs_channel.send(f'!cube Fellowship {member.mention}')
        #             time.sleep(0.5)
        # await self.message_members_not_in_league("Wilds")

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Booster tutor adds sealeddeck.tech links as part of an edit operation
        if before.author == self.booster_tutor:
            if before.channel == self.pool_channel and "Sealeddeck.tech link" not in before.content and\
                    "Sealeddeck.tech link" in after.content:
                # Edit adds a sealeddeck link
                await self.track_starting_pool(after)
                return
            elif (before.channel == self.packs_channel and len(before.embeds) and len(after.embeds)
                    and not any(filter(lambda f: f.name == "Sealeddeck.tech ID", before.embeds[0].fields))
                    and any(filter(lambda f: f.name == "Sealeddeck.tech ID", after.embeds[0].fields))):
                # track multiple packs in pack-gen channel
                await self.track_pack(after)
                return

    async def on_message(self, message: discord.Message):
        # As part of the !playerchoice flow, repost Booster Tutor packs in pack-generation with instructions for
        # the appropriate user to select their pack.
        if (message.channel == self.bot_bunker_channel and message.author == self.booster_tutor
                and message.mentions[0] == self.user):
            await self.handle_booster_tutor_response(message)
            return

        if message.author == self.booster_tutor:
            if message.channel == self.packs_channel and len(message.embeds) and message.embeds[0].description and "```" in message.embeds[0].description:
                # Message is a generated pack
                await self.track_pack(message)
                return

        # Split the string on the first space
        argv = message.content.split(None, 1)
        if len(argv) == 0:
            return
        command = argv[0].lower()
        argument = ''
        if '"' in message.content:
            # Support arguments passed in quotes
            argument = message.content.split('"')[1]
        elif ' ' in message.content:
            argument = argv[1]

        if not message.guild:
            # For now, only allow Sawyer to send broadcasts
            if command == '!messagetest' and 346124470940991488 == message.author.id:
                await self.message_members_not_in_league(message.content.split(' ')[1], argument, message.author, True)
                return

            if command == '!realmessageiambeingverycareful' and 346124470940991488 == message.author.id:
                await self.message_members_not_in_league(message.content.split(' ')[1], argument, message.author)
                return

            if message.author == self.user:
                return
            await self.on_dm(message, command, argument)
            return

        if command == '!playerchoice' and message.channel == self.packs_channel:
            await self.prompt_user_pick(message)
            return

        if command == '!addpack' and message.reference:
            await self.add_pack(message, argument)
            return

        # if command == '!explore' and message.channel == self.packs_channel:
        #     await self.explore(message)
        #     return

        if command == '!collect' and message.channel == self.packs_channel:
            await self.collect(message, argument)

        if command == '!randint':
            args = argv[1].split(None)
            if len(args) == 1:
                await message.channel.send(
                    f"{random.randint(1, int(args[0]))}"
                )
            else:
                await message.channel.send(
                    f"{random.randint(int(args[0]), int(args[1]))}"
                )
            return

        if message.channel == self.lfm_channel and command == '!challenge':
            await self.issue_challenge(message)
        elif message.channel == self.duel_channel and command == '!challenge':
            await self.issue_duel_challenge(message)
        elif command == '!help':
            await message.channel.send(
                f"You can give me one of the following commands:\n"
                f"> `!challenge`: Challenges the current player in the LFM (or duel) queue\n"
                f"> `!randint A B`: Generates a random integer n, where A <= n <= B. If only one input is given, "
                f"uses that value as B and defaults A to 1. \n "
                f"> `!help`: shows this message\n"
            )

    async def collect(self, message: discord.Message, argument: str):
        allowed_sets = ["mkm", "lci", "woe", "mom", "one", "bro"]
        try:
            args = argument.split(' ')
            clues_to_spend = int(args[0])
            sets = args[1:]
        except ValueError:
            await message.reply("Hmm, I don't understand that. Try `!collect 2`, `!collect 4 SET`, `!collect 6 SET SET`, or `!collect 10 SET SET`. (SET can be `mkm`, `lci`, `woe`, `mom`, `one`, or `bro`. You can choose the same set twice.)")
            return
        if clues_to_spend not in [2,4,6,10]:
            await message.reply("You can only use 2, 4, 6, or 10 clues when collecting evidence.")
            return
        if clues_to_spend == 2 and len(sets) != 0:
            await message.reply("Hmm, I don't understand that. Try `!collect 2`, `!collect 4 SET`, `!collect 6 SET SET`, or `!collect 10 SET SET`. (SET can be `mkm`, `lci`, `woe`, `mom`, `one`, or `bro`. You can choose the same set twice.)")
            return
        if clues_to_spend == 4 and (len(sets) != 1 or sets[0].lower() not in allowed_sets):
            await message.reply("Hmm, I don't understand that. Try `!collect 2`, `!collect 4 SET`, `!collect 6 SET SET`, or `!collect 10 SET SET`. (SET can be `mkm`, `lci`, `woe`, `mom`, `one`, or `bro`. You can choose the same set twice.)")
            return
        if clues_to_spend in [6, 10] and (len(sets) != 2 or sets[0].lower() not in allowed_sets or sets[1].lower() not in allowed_sets):
            await message.reply("Hmm, I don't understand that. Try `!collect 2`, `!collect 4 SET`, `!collect 6 SET SET`, or `!collect 10 SET SET`. (SET can be `mkm`, `lci`, `woe`, `mom`, `one`, or `bro`. You can choose the same set twice.)")
            return

        sets = [s.lower() if s.lower() != "mkm" else "a-mkm" for s in sets]

        last_6 = "!from a-mkm|lci|woe|mom|one|bro"

        # Get sealeddeck link and loss count from spreadsheet
        spreadsheet_values = await self.get_spreadsheet_values('Pools!B7:AA200')
        curr_row = 6
        for row in spreadsheet_values:
            curr_row += 1
            if len(row) < 5:
                continue
            if row[0].lower() != '' and row[0].lower() in message.author.display_name.lower():
                losses = int(row[2])
                clues_available = int(row[15])
                if clues_available < clues_to_spend:
                    await message.reply(f'By my records, you do not have enough clues. If this is in error, '
                                        f'please post in {self.league_committee_channel.mention}')
                    return

                if losses == 0:
                    await message.reply(f'It looks like you don\'t have a pack to reroll yet. If this is in error, '
                                        f'please post in {self.league_committee_channel.mention}')
                    return

                # Mark the clues as used
                self.sheet.values().update(spreadsheetId=self.spreadsheet_id,
                                           range=f'Pools!R{curr_row}:R{curr_row}', valueInputOption='USER_ENTERED',
                                           body={'values': [[int(row[16]) + clues_to_spend]]}).execute()

                if clues_to_spend == 2:
                    await self.packs_channel.send(f"{last_6} {message.author.mention}")
                elif clues_to_spend == 4:
                    await self.packs_channel.send(f"!from {'|'.join(sets)} {message.author.mention}")
                elif clues_to_spend == 6:
                    # ripped from prompt_user_pick
                    while self.awaiting_boosters_for_user is not None:
                        time.sleep(3)

                    booster_one_type = f"!{sets[0]}"
                    booster_two_type = f"!{sets[1]}"
                    self.num_boosters_awaiting = 2
                    self.awaiting_boosters_for_user = message.author

                    # Generate two packs of the specified types
                    await self.bot_bunker_channel.send(booster_one_type)
                    await self.bot_bunker_channel.send(booster_two_type)
                elif clues_to_spend == 10:
                    self.double_packs[message.author.id] = []
                    await self.packs_channel.send(f"!{sets[0]} {message.author.mention}")
                    await self.packs_channel.send(f"!{sets[1]} {message.author.mention}")
                    # TODO MKM replace pack with both???
                return
        await message.reply(f'Hmm, I can\'t find you in the league spreadsheet. '
                            f'Please post in {self.league_committee_channel.mention}')

    async def explore(self, message: discord.Message):
        possible_sets = [
            "SIR",
            "AKR",
            "KLR",
            "WOE",
            "MOM",
            "ONE",
            "BRO",
            "DMU",
            "SNC",
            "NEO",
            "VOW",
            "MID",
            "AFR",
            "STX",
            "KHM",
            "ZNR",
            "M21",
            "IKO",
            "THB",
            "ELD",
            "M20",
            "WAR",
            "RNA",
            "GRN",
            "M19",
            "DOM",
            "RIX",
            "XLN",
        ]
        set_to_generate = random.choice(possible_sets)
        # Get sealeddeck link and loss count from spreadsheet
        spreadsheet_values = await self.get_spreadsheet_values('Pools!B7:R200')
        curr_row = 6
        for row in spreadsheet_values:
            curr_row += 1
            if len(row) < 5:
                continue
            if row[0].lower() != '' and row[0].lower() in message.author.display_name.lower():
                if int(row[16]) <= 0:
                    await message.reply(f'By my records, you do not have any unused maps. If this is in error, '
                                        f'please post in {self.league_committee_channel.mention}')
                    return

                # Mark the map as used
                self.sheet.values().update(spreadsheetId=self.spreadsheet_id,
                                           range=f'Pools!Q{curr_row}:Q{curr_row}', valueInputOption='USER_ENTERED',
                                           body={'values': [[int(row[15]) + 1]]}).execute()

                # Roll a new pack
                await self.packs_channel.send(
                    f'!{set_to_generate} {message.author.mention} follows a map to uncharted territory')
                return
        await message.reply(f'Hmm, I can\'t find you in the league spreadsheet. '
                            f'Please post in {self.league_committee_channel.mention}')

    async def track_starting_pool(self, message: discord.Message):
        # Handle cases where Booster Tutor fails to generate a sealeddeck.tech link
        if '**Sealeddeck.tech:** Error' in message.content:
            # TODO: highlight the pool cell red and DM someone if this happens
            return

        # Use a regex to pull the sealeddeck id out of the message
        sealed_deck_id = \
            re.search("(?P<url>https?://[^\s]+)", message.content).group("url").split('sealeddeck.tech/')[1]
        sealed_deck_link = f'https://sealeddeck.tech/{sealed_deck_id}'

        spreadsheet_values = await self.get_spreadsheet_values('Pools!B7:F200')

        curr_row = 6
        for row in spreadsheet_values:
            curr_row += 1
            if len(row) < 1:
                continue
            if row[0].lower() != '' and row[0].lower() in message.mentions[0].display_name.lower():
                # Update the proper cell in the spreadsheet
                body = {
                    'values': [
                        # [f'=HYPERLINK("{sealed_deck_link}", "Link")', f'=HYPERLINK("{sealed_deck_link}", "Link")'],
                        [sealed_deck_link, sealed_deck_link],
                    ],
                }
                self.sheet.values().update(spreadsheetId=self.spreadsheet_id,
                                           range=f'Pools!E{curr_row}:F{curr_row}', valueInputOption='USER_ENTERED',
                                           body=body).execute()
                self.sheet.values().update(spreadsheetId=self.spreadsheet_id,
                                           range=f'Pools!S{curr_row}:S{curr_row}', valueInputOption='USER_ENTERED',
                                           body={'values': [[sealed_deck_link]]}).execute()

                return
        # TODO do something if the value could not be found
        return

    async def track_pack(self, message: discord.Message):
        """
        Track a pack in the Pools tab. This assumes the pack's owner is the last mention in the message, and that the pack contents is in a code fence.
        """

        content = message.embeds[0].description
        ref = await message.channel.fetch_message(message.reference.message_id)
        pack_owner_user_id_match = re.search("<@!?(?P<id>\\d+)>", ref.content)
        pack_owner_user_id = pack_owner_user_id_match and pack_owner_user_id_match.group("id")

        # Get (starting) pool links & player info from spreadsheet
        pools = await self.get_spreadsheet_values('Pools!C7:H')
        # Get pool changes from spreadsheet
        changes = await self.get_spreadsheet_values('Pool Changes!B2:D')
        (row_num, values) = next(((i + 7, vals) for i, vals in enumerate(pools) if vals[0] == pack_owner_user_id), (None, None))
        if row_num is None or values is None:
            # This should only happen during debugging / spreadsheet setup
            print(f"rut row. No pool found for {pack_owner_user_id}")
            return

        name = values[1]
        current_pool = values[4]
        starting_pool = values[5]

        # either it's a single pack or there's a Sealeddeck ID
        if content and "```" in content:
            pack_content = content.split("```")[1].strip()
            pack_json = arena_to_json(pack_content)
        else:
            field = next(filter(lambda f: f.name == "Sealeddeck.tech ID", message.embeds[0].fields))
            pack_json = await sealeddeck_pool(field.value.replace("`", ""))

        # If this is a double pack, wait for the second pack to be resolved, then treat both as one
        if pack_owner_user_id and pack_owner_user_id in self.double_packs:
            double_pack = self.double_packs[pack_owner_user_id]
            if len(double_pack) == 0:
                double_pack.append(pack_json)
                return
            else:
                pack_json = [*double_pack[0], *pack_json]
                del self.double_packs[pack_owner_user_id]

        try:
            new_pack_id = await pool_to_sealeddeck(pack_json)
        except:
            print("sealeddeck issue — generating pack")
            # If something goes wrong with sealeddeck, highlight the pack cell red
            await self.set_cell_to_red(row_num, 'G')
            return

        await self.write_pack(name, new_pack_id)

        if current_pool == '':
            await self.set_cell_to_red(curr_row, 'G')
            return

        try:
            pool = await self.pool_from_changes([row for row in changes if row[0] == name])
            pool = [*pool, *pack_json]
            # TODO detect if it's just adding new cards; if so, rebuild off current_pool instead of starting_pool; displays more nicely
            updated_pool_id = await pool_to_sealeddeck(pool, starting_pool.split('.tech/')[1])

        except:
            print("sealeddeck issue — updating pool")
            # If something goes wrong with sealeddeck, highlight the pack cell red
            await self.set_cell_to_red(row_num, 'G')
            return

        # Write updated extra-card-included pool to spreadsheet
        pool_body = {
            'values': [
                [f'https://sealeddeck.tech/{updated_pool_id}'],
            ],
        }
        self.sheet.values().update(spreadsheetId=self.spreadsheet_id,
                                   range=f'Pools!G{row_num}', valueInputOption='USER_ENTERED',
                                   body=pool_body).execute()
        return

    async def pool_from_changes(self, changes: Sequence[Tuple[str, str, str]]) -> Sequence[SealedDeckEntry]:
        packs = []
        cards = defaultdict(int)

        for (_name, operation, value) in changes:
            if operation == "add pack":
                packs.append(value)
            elif operation == "remove pack":
                packs.remove(value)
            elif operation == "add card":
                cards[value] += 1
            elif operation == "remove card":
                cards[value] -= 1

        pack_contents = []
        for id in packs:
            pack_contents.append(await sealeddeck_pool(id))
        for pack in pack_contents:
            if pack:
                for card in pack:
                    cards[card["name"]] += card["count"]

        return [{"name": name, "count": count} for name, count in cards.items() if count > 0]

    async def write_pack(self, name: str, new_pack_id: str):
        pack_body = {
            'values': [
                [datetime.now().isoformat(),name,"add pack",new_pack_id],
            ],
        }
        # Find the proper column ID
        self.sheet.values().append(spreadsheetId=self.spreadsheet_id,
                                   range=f'Pool Changes!A:D', valueInputOption='USER_ENTERED',
                                   body=pack_body).execute()

    async def set_cell_to_red(self, row: int, col: str):
        # Note that this request (annoyingly) uses indices instead of the regular cell format.
        color_body = {
            'requests': [{
                'updateCells': {
                    'rows': [{
                        'values': [{
                            'userEnteredFormat': {
                                'backgroundColorStyle': {
                                    'rgbColor': {
                                        "red": 1,
                                        "green": 0,
                                        "blue": 0,
                                        "alpha": 1,
                                    }
                                }
                            }
                        }]
                    }],
                    'fields': 'userEnteredFormat',
                    'range': {
                        'sheetId': self.pools_tab_id,
                        'startRowIndex': row - 1,
                        'endRowIndex': row,
                        'startColumnIndex': ord(col) - ord('A'),
                        'endColumnIndex': ord(col) - ord('A') + 1,
                    },
                },
            }],
        }
        self.sheet.batchUpdate(spreadsheetId=self.spreadsheet_id,
                               body=color_body).execute()

    async def prompt_user_pick(self, message: discord.Message):
        # # Ensure the user doesn't already have a pending pick to make
        # pendingPickMessage = await self.packs_channel.history().find(
        # 	lambda m : m.author.name == 'AGL Bot'
        # 	and m.mentions
        # 	and m.mentions[0] == message.mentions[0]
        # 	and f'Pack Option' in m.content
        # 	)
        # if (pendingPickMessage):
        # 	await self.packs_channel.send(
        # 		f'{message.mentions[0].mention} You still have a pending pack selection to make! Please select your '
        # 		f'previous pack, and then post in #league-committee so someone can can manually generate your new packs.'
        # 	)
        # 	return

        # Messages from Booster Tutor aren't tied to a user, so only one pair can be resolved at a time.
        while self.awaiting_boosters_for_user is not None:
            time.sleep(3)

        booster_one_type = message.content.split(None)[1]
        booster_two_type = message.content.split(None)[2]
        self.num_boosters_awaiting = 2
        self.awaiting_boosters_for_user = message.mentions[0]

        # Generate two packs of the specified types
        await self.bot_bunker_channel.send(booster_one_type)
        await self.bot_bunker_channel.send(booster_two_type)

    async def handle_booster_tutor_response(self, message: discord.Message):
        assert self.num_boosters_awaiting > 0
        if self.num_boosters_awaiting == 2:
            self.num_boosters_awaiting -= 1
            await self.packs_channel.send(
                f'Pack Option A for {self.awaiting_boosters_for_user.mention}. To select this pack, DM me '
                f'`!choosePackA`\n '
                f'```{message.content.split("```")[1].strip()}```')
        else:
            self.num_boosters_awaiting -= 1
            await self.packs_channel.send(
                f'Pack Option B for {self.awaiting_boosters_for_user.mention}. To select this pack, DM me '
                f'`!choosePackB`\n '
                f'```{message.content.split("```")[1].strip()}```')
        if self.num_boosters_awaiting == 0:
            self.awaiting_boosters_for_user = None

    async def issue_duel_challenge(self, message: discord.Message):
        if not self.pending_duel_user_mention:
            await self.duel_channel.send(
                "Sorry, but no one is looking for a match right now. You can request an anonymous duel by DMing me "
                "`!duel`. "
            )
            return

        standings_data = await self.get_spreadsheet_values("Standings!E6:T")

        pending_user_row = next(filter(lambda r: len(r) and r[-1] == str(self.pending_duel_user_id), standings_data), None)
        challenger_row = next(filter(lambda r: len(r) and r[-1] == str(message.author.id), standings_data), None)

        pending_user_team = pending_user_row and pending_user_row[0] and f" ({pending_user_row[0]})" or ""
        challenger_team = challenger_row and challenger_row[0] and f" ({challenger_row[0]})" or ""

        await self.duel_channel.send(
            f"{self.pending_duel_user_mention}{pending_user_team}, your anonymous duel has been accepted by {message.author.mention}{challenger_team}.")

        await update_message(
            self.active_duel_message,
            f'~~{self.active_duel_message.content}~~\n'
            f'A match was found between {self.pending_duel_user_mention}{pending_user_team} and {message.author.mention}{challenger_team}.'
        )

        self.pending_duel_user_mention = None
        self.pending_duel_user_id = None
        self.active_duel_message = None

    async def issue_challenge(self, message: discord.Message):
        if not self.pending_lfm_user_mention:
            await self.lfm_channel.send(
                "Sorry, but no one is looking for a match right now. You can send out an anonymous LFM by DMing me "
                "`!lfm`. "
            )
            return

        player_data = await self.get_spreadsheet_values("Player Database!D2:W")

        pending_user_row = next(filter(lambda r: len(r) and r[0] == str(self.pending_lfm_user_id), player_data), None)
        challenger_row = next(filter(lambda r: len(r) and r[0] == str(message.author.id), player_data), None)

        pending_user_dread = pending_user_row and pending_user_row[-1] and f" (Dread: {pending_user_row[-1]})" or ""
        challenger_dread = challenger_row and challenger_row[-1] and f" (Dread: {challenger_row[-1]})" or ""

        await self.lfm_channel.send(
            f"{self.pending_lfm_user_mention}{pending_user_dread}, your anonymous LFM has been accepted by {message.author.mention}{challenger_dread}.")

        await update_message(
            self.active_lfm_message,
            f'~~{self.active_lfm_message.content}~~\n'
            f'A match was found between {self.pending_lfm_user_mention}{pending_user_dread} and {message.author.mention}{challenger_dread}.'
        )

        self.pending_lfm_user_mention = None
        self.pending_lfm_user_id = None
        self.active_lfm_message = None

    async def choose_pack(self, user: Union[discord.Member, discord.User], chosen_option: str):
        if chosen_option == 'A':
            not_chosen_option = 'B'
            split = '!choosePackA`'
            not_chosen_split = '!choosePackB`'
        else:
            not_chosen_option = 'A'
            split = '!choosePackB`'
            not_chosen_split = '!choosePackA`'
        chosen_message = None
        async for message in self.packs_channel.history(limit=500):
            if (message.author.name == 'AGL Bot' and message.mentions and message.mentions[0] == user
                    and f'Pack Option {chosen_option}' in message.content):
                chosen_message = message
                break

        not_chosen_message = None
        async for message in self.packs_channel.history(limit=500):
            if (message.author.name == 'AGL Bot' and message.mentions and message.mentions[0] == user
                    and f'Pack Option {not_chosen_option}' in message.content):
                not_chosen_message = message
                break

        if not chosen_message or not not_chosen_message:
            await user.send(
                f"Sorry, but I couldn't find any pending packs for you. Please post in "
                f"{self.league_committee_channel.mention} if you think this is an error.")
            return

        chosen_message_text = f'Pack chosen by {user.mention}.{chosen_message.content.split(split)[1]}'

        chosen_message = await update_message(chosen_message, chosen_message_text)

        not_chosen_message = await update_message(not_chosen_message,
                                                  f'Pack not chosen by {user.mention}.'
                                                  f'~~{not_chosen_message.content.split(not_chosen_split)[1]}~~')

        await user.send("Understood. Your selection has been noted.")

        # TODO this likely breaks because booster tutor messages and ours don't follow the same format anymore (embed vs content)
        await self.track_pack(chosen_message)

        return

    async def on_dm(self, message: discord.Message, command: str, argument: str):
        if command == '!choosepacka' or command == '!chooseurza':
            await self.choose_pack(message.author, 'A')
            return

        if command == '!choosepackb' or command == '!choosemishra':
            await self.choose_pack(message.author, 'B')
            return

        if command == '!duel':
            if self.pending_duel_user_mention:
                await message.author.send(
                    "Someone is already looking for a duel. You can play them by posting !challenge in the "
                    "looking-for-matches channel of the league discord. "
                )
                return
            if not argument:
                self.active_duel_message = await self.duel_channel.send(
                    "An anonymous player is looking for a duel. Post `!challenge` to reveal their identity and "
                    "initiate a match. "
                )
            else:
                self.active_duel_message = await self.duel_channel.send(
                    f"An anonymous player is looking for a duel. Post `!challenge` to reveal their identity and "
                    f"initiate a match.\n "
                    f"Message from the player:\n"
                    f"> {argument}"
                )
            await message.author.send(
                f"I've created a post for you. You'll receive a mention when an opponent is found.\n"
                f"If you want to cancel this, send me a message with the text `!nvm`."
            )
            self.pending_duel_user_mention = message.author.mention
            self.pending_duel_user_id = message.author.id
            return

        if command == '!lfm':
            if self.pending_lfm_user_mention:
                await message.author.send(
                    "Someone is already looking for a match. You can play them by posting !challenge in the "
                    "looking-for-matches channel of the league discord. "
                )
                return
            if not argument:
                self.active_lfm_message = await self.lfm_channel.send(
                    "An anonymous player is looking for a match. Post `!challenge` to reveal their identity and "
                    "initiate a match. "
                )
            else:
                self.active_lfm_message = await self.lfm_channel.send(
                    f"An anonymous player is looking for a match. Post `!challenge` to reveal their identity and "
                    f"initiate a match.\n "
                    f"Message from the player:\n"
                    f"> {argument}"
                )
            await message.author.send(
                f"I've created a post for you. You'll receive a mention when an opponent is found.\n"
                f"If you want to cancel this, send me a message with the text `!nvm`."
            )
            self.pending_lfm_user_mention = message.author.mention
            self.pending_lfm_user_id = message.author.id
            return

        if command == '!retractlfm' or command == '!nvm':
            handled = False
            if message.author.mention == self.pending_lfm_user_mention:
                await self.active_lfm_message.delete()
                self.active_lfm_message = None
                await message.author.send(
                    "Understood. The post made on your behalf has been deleted."
                )
                self.pending_lfm_user_mention = None
                self.pending_lfm_user_id = None
                handled = True
            if message.author.mention == self.pending_duel_user_mention:
                await self.active_duel_message.delete()
                self.active_duel_message = None
                await message.author.send(
                    "Understood. The post made on your behalf has been deleted."
                )
                self.pending_duel_user_mention = None
                self.pending_duel_user_id = None
                handled = True
            if not handled:
                await message.author.send(
                    "You don't currently have an outgoing LFM or duel."
                )
            return

        await message.author.send(
            f"I'm sorry, but I didn't understand that. Please send one of the following commands:\n"
            f"> `!lfm`: creates an anonymous post looking for a match.\n"
            f"> `!duel`: creates an anonymous post looking for a duel.\n"
            f"> `!nvm`: removes an anonymous LFM or duel that you've sent out."
            f"> `!choosePackA`: responds to a pending pack selection option."
            f"> `!choosePackB`: responds to a pending pack selection option."
        )

    async def add_pack(self, message: discord.Message, argument: str):
        if message.channel != self.packs_channel:
            return

        ref = await message.channel.fetch_message(
            message.reference.message_id
        )
        if ref.author == self.booster_tutor:
            return
        if ref.author != self.user:
            await message.channel.send(
                f"{message.author.mention}\n"
                "The message you are replying to does not contain packs I have generated"
            )

        pack_content = ref.content.split("```")[1].strip()
        sealeddeck_id = argument.strip()
        pack_json = arena_to_json(pack_content)
        m = await message.channel.send(
            f"{message.author.mention}\n"
            f":hourglass: Adding pack to pool..."
        )
        try:
            new_id = await pool_to_sealeddeck(
                pack_json, sealeddeck_id
            )
        except aiohttp.ClientResponseError as e:
            print(f"Sealeddeck error: {e}")
            content = (
                f"{message.author.mention}\n"
                f"The packs could not be added to sealeddeck.tech "
                f"pool with ID `{sealeddeck_id}`. Please, verify "
                f"the ID.\n"
                f"If the ID is correct, sealeddeck.tech might be "
                f"having some issues right now, try again later."
            )

        else:
            content = (
                f"{message.author.mention}\n"
                f"The packs have been added to the pool.\n\n"
                f"**Updated sealeddeck.tech pool**\n"
                f"link: https://sealeddeck.tech/{new_id}\n"
                f"ID: `{new_id}`"
            )
        await m.edit(content=content)

    async def print_members_not_in_league(self, league_name: str):
        for member in self.guilds[0].members:
            found = False
            if member.bot:
                continue
            for role in member.roles:
                if league_name in role.name:
                    found = True
            if not found:
                print(member.display_name)

    async def message_members(self):
        for member in self.guilds[0].members:
            if member.display_name in 'put names here':
                print('trying to DM: ' + member.display_name)
                # if 'Sawyer T' in member.display_name:
                await message_member(member)
                print('DMed ' + member.display_name)

    async def message_members_not_in_league(self, league_name: str, content: str, sender: Union[discord.Member, discord.User], test_mode=False):
        count = 0
        if test_mode:
            await message_member(sender, content)
            count += 1
        else:
            for member in self.guilds[0].members:
                found = False
                if member.bot:
                    continue
                for role in member.roles:
                    if league_name in role.name:
                        found = True
                if not found:
                    print('trying to DM: ' + member.display_name)
                    await message_member(member, content)
                    print('DMed ' + member.display_name)
                    count += 1
        await sender.send(f'Successfully DMed {count} user(s).')

    async def get_spreadsheet_values(self, range: str, valueRenderOption="FORMATTED_VALUE"):
        creds = None
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json',
                                                          ['https://www.googleapis.com/auth/spreadsheets'])
        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', ['https://www.googleapis.com/auth/spreadsheets'])
                creds = flow.run_local_server(port=0)
            # Save the credentials for the next run
            with open('token.json', 'w') as token:
                token.write(creds.to_json())

        try:
            service = build('sheets', 'v4', credentials=creds)

            # Call the Sheets API
            self.sheet = service.spreadsheets()
            result = self.sheet.values().get(spreadsheetId=self.spreadsheet_id,
                                             range=range,
                                             valueRenderOption=valueRenderOption).execute()
            return result.get('values', [])
        except HttpError as err:
            print(err)
        return []
