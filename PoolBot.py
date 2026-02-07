from __future__ import print_function
# bot.py
import os

import discord
import re
import random
import ssl
import time
from dotenv import load_dotenv
from typing import Optional, Sequence, Union, List, TypedDict, Tuple
from datetime import datetime
from collections import Counter, defaultdict
from asyncio import Lock, sleep

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

def choose_stip():
    stips = ["Pauper", "Companion: Gyruda", "Companion: Obosh", "Companion: Keruga", "Companion: Lurrus", "Companion: Yorion"]
    stip = random.choice(stips)
    return "\nStipulation: " + stip

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
    deck: dict[str, Union[Sequence[SealedDeckEntry], str]] = {"sideboard": punishment_cards}
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

async def get_sheet_client():
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
        return service.spreadsheets()
    except HttpError as err:
        print(err)
    return None

async def get_spreadsheet_values(sheet, spreadsheet_id: str, range: str, valueRenderOption="FORMATTED_VALUE"):
    retries = 0
    while retries < 5:
        try:
            # Call the Sheets API
            result = sheet.values().get(spreadsheetId=spreadsheet_id,
                                        range=range,
                                        valueRenderOption=valueRenderOption).execute()
            return result.get('values', [])
        except ssl.SSLEOFError:
            retries += 1
            await sleep(retries)
            continue
        except HttpError as err:
            print(err)
        return []

async def set_cell_to_red(sheet, spreadsheet_id: str, tab_id: str, row: int, col: str):
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
                    'sheetId': tab_id,
                    'startRowIndex': row - 1,
                    'endRowIndex': row,
                    'startColumnIndex': ord(col) - ord('A'),
                    'endColumnIndex': ord(col) - ord('A') + 1,
                },
            },
        }],
    }
    sheet.batchUpdate(spreadsheetId=spreadsheet_id,
                      body=color_body).execute()

class PoolTracker():
    def __init__(self, pool_channel: discord.TextChannel, packs_channel: discord.TextChannel, spreadsheet_id: str, tab_id: str):
        self.pool_channel = pool_channel
        self.packs_channel = packs_channel
        self.spreadsheet_id = spreadsheet_id
        self.tab_id = tab_id
        self.pool_lock = Lock()
        self.sheet = None
        pass

    async def track_pack(self, message: discord.Message):
        """
        Track a pack in the Pools tab. This assumes the pack's owner is the last mention in the message, and that the pack contents is in a code fence.
        """

        sheet = self.sheet or await get_sheet_client()
        if not self.sheet:
            self.sheet = sheet

        async with self.pool_lock:
            content = message.embeds[0].description
            ref = message.reference and message.reference.message_id and await message.channel.fetch_message(message.reference.message_id)
            pack_owner_user_id_match = ref and re.search("<@!?(?P<id>\\d+)>", ref.content)
            pack_owner_user_id = pack_owner_user_id_match and pack_owner_user_id_match.group("id")

            # Get pool changes from spreadsheet
            changes = await get_spreadsheet_values(sheet, self.spreadsheet_id, 'Pool Changes!B2:F')

            # find name from player db (name is column B, discord ID is column F)
            player_data = await get_spreadsheet_values(sheet, self.spreadsheet_id, "Player Database!B2:F")
            (player_row_index, player_row) = next(filter(lambda r: len(r[1]) > 4 and r[1][4] == str(pack_owner_user_id), enumerate(player_data)), (None, None))

            if player_row is None or player_row_index is None:
                # This should only happen during debugging / spreadsheet setup
                print(f"rut row. No pool found for {pack_owner_user_id}")
                return
            
            # pool row starts at 7 (1-indexed), player rows start at 0, so add 7 to the index
            row_num = player_row_index + 7
            name = player_row[0]

            # current pool is last pool in the changes that matches the player name; name column is B and pool id is  so index is 4
            current_pool_id = ''
            for change in changes:
                if len(change) >= 5 and change[0] == name and change[4]:
                    current_pool_id = change[4]

            if current_pool_id == '':
                print(f"rut row. No pool found for {name}")
                await self.set_cell_to_red(row_num, 'G')
                return

            # either it's a single pack or there's a Sealeddeck ID
            if content and "```" in content:
                pack_content = content.split("```")[1].strip()
                pack_json = arena_to_json(pack_content)
            else:
                field = next(filter(lambda f: f.name == "SealedDeck.Tech ID", message.embeds[0].fields))
                pack_json = await sealeddeck_pool(field.value.replace("`", ""))

            try:
                new_pack_id = await pool_to_sealeddeck(pack_json)
            except:
                print("sealeddeck issue — generating pack")
                # If something goes wrong with sealeddeck, highlight the pack cell red
                await self.set_cell_to_red(row_num, 'G')
                return
 
            try:
                updated_pool_id = await pool_to_sealeddeck(pack_json, current_pool_id)

            except:
                print("sealeddeck issue — updating pool")
                # If something goes wrong with sealeddeck, highlight the pack cell red
                await self.set_cell_to_red(row_num, 'G')
                return

            try:
                await self.write_pack(name, new_pack_id, updated_pool_id)
            except:
                print("sealeddeck issue — writing pack")
                # If something goes wrong with sealeddeck, highlight the pack cell red
                await self.set_cell_to_red(row_num, 'G')
                return

    async def write_pack(self, name: str, new_pack_id: str, updated_pool_id: str):
        sheet = self.sheet or await get_sheet_client()
        if not self.sheet:
            self.sheet = sheet

        pack_body = {
            'values': [
                [datetime.now().isoformat(),name,"add pack",new_pack_id,"",updated_pool_id],
            ],
        }
        # Find the proper column ID
        sheet.values().append(spreadsheetId=self.spreadsheet_id,
                                   range=f'Pool Changes!A:D', valueInputOption='USER_ENTERED',
                                   body=pack_body).execute()

    async def set_cell_to_red(self, row: int, col: str):
        await set_cell_to_red(self.sheet, self.spreadsheet_id, self.tab_id, row, col)

class Matchmaker():
    def __init__(self, command: str, what_it_is: str, channel: discord.TextChannel, spreadsheet_id: str, extra=None):
        self.command = command
        self.what_it_is = what_it_is
        self.channel = channel
        self.spreadsheet_id = spreadsheet_id
        self.extra = extra
        self.sheet = None
        self.pending_user_mention: Optional[str] = None
        self.pending_user_id: Optional[str] = None
        self.active_message: Optional[discord.Message] = None

    async def issue_challenge(self, message: discord.Message):
        if not self.pending_user_mention:
            await self.channel.send(
                f"Sorry, but no one is looking for {self.what_it_is} right now. You can send out an anonymous LFM by DMing me "
                f"`{self.command}`. "
            )
            return

        async with self.channel.typing():
            sheet = self.sheet or await get_sheet_client()
            if not self.sheet:
                self.sheet = sheet

            player_data = await get_spreadsheet_values(sheet, self.spreadsheet_id, "Player Database!C2:W")

            factions = {
                "Fire Nation": "🔥",
                "Water Tribes": "🌊",
                "Air Nomads": "🌪️",
                "Order of the White Lotus": "🪷",
                "Swampbenders": "🍄",
                "Kyoshi Warriors": "🪭",
                "Dai Li": "🕵️",
                "Sun Warriors": "🌞",
            }

            pending_user_row = next(filter(lambda r: len(r) > 3 and r[3] == str(self.pending_user_id), player_data), None)
            challenger_row = next(filter(lambda r: len(r) > 3 and r[3] == str(message.author.id), player_data), None)

            pending_user_extra = "" # pending_user_row and pending_user_row[0] and pending_user_row[0] in factions and factions[pending_user_row[0]] or ""
            challenger_extra =  "" # challenger_row and challenger_row[0] and challenger_row[0] in factions and factions[challenger_row[0]] or ""

            overall_extra = self.extra() if self.extra else ""

            await self.channel.send(
                f"{self.pending_user_mention}{pending_user_extra}, your anonymous LFM has been accepted by {message.author.mention}{challenger_extra}.{overall_extra}")

            await update_message(
                self.active_message,
                f'~~{self.active_message.content}~~\n'
                f'A match was found between {self.pending_user_mention}{pending_user_extra} and {message.author.mention}{challenger_extra}.'
            )

        self.pending_user_mention = None
        self.pending_user_id = None
        self.active_message = None

    async def handle_command(self, message: discord.Message, argument: str):
        if self.pending_user_mention:
            await message.author.send(
                f"Someone is already looking for {self.what_it_is}. You can play them by posting `!challenge` in {self.channel.jump_url}"
            )
            return
        if not argument:
            self.active_message = await self.channel.send(
                f"An anonymous player is looking for {self.what_it_is}. Post `!challenge` to reveal their identity and "
                f"initiate {self.what_it_is}. "
            )
        else:
            self.active_message = await self.channel.send(
                f"An anonymous player is looking for {self.what_it_is}. Post `!challenge` to reveal their identity and "
                f"initiate {self.what_it_is}.\n "
                f"Message from the player:\n"
                f"> {argument}"
            )
        await message.author.send(
            f"I've created a post for you: {self.active_message.jump_url}\n"
            "You'll receive a mention when an opponent is found.\n"
            f"If you want to cancel this, send me a message with the text `!nvm`."
        )
        self.pending_user_mention = message.author.mention
        self.pending_user_id = message.author.id

    async def handle_retract(self, message: discord.Message):
        if message.author.mention == self.pending_user_mention:
            await self.active_message.delete()
            self.active_message = None
            await message.author.send(
                f"Understood. The post made on your behalf in {self.channel.jump_url} has been deleted."
            )
            self.pending_user_mention = None
            self.pending_user_id = None
            return True
        return False

def has_pack(message: discord.Message):
    def has_right_name(f #: discord._EmbedFieldProxy
        ):
        return f.name == "SealedDeck.Tech ID"
    return len(message.embeds) and ((message.embeds[0].description and "```" in message.embeds[0].description) or (any(filter(has_right_name, message.embeds[0].fields))))

class PoolBot(discord.Client):
    def __init__(self, config: utils.Config, intents: discord.Intents, *args, **kwargs):
        self.config = config
        self.league_start = datetime.fromisoformat('2022-06-22')
        super().__init__(intents=intents, *args, **kwargs)

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        if not self.config.skip_username:
            await self.user.edit(username='AGL Bot')
        # If this is true, posts will be limited to #bot-lab and #bot-bunker, and LFM DMs will be ignored.
        self.dev_mode = self.config.debug_mode == "active"
        self.pools_tab_id = self.config.pools_tab_id
        self.pool_channel = self.get_channel(719933932690472970) if not self.dev_mode else self.get_channel(
            1065100936445448232)
        self.packs_channel = self.get_channel(798002275452846111) if not self.dev_mode else self.get_channel(
            1065101003168436295)
        self.second_packs_channel = self.get_channel(935295982596423711) if not self.dev_mode else self.get_channel(
            1065101003168436295) # TODO seaprate dev mode channel?
        self.lfm_channel = self.get_channel(720338190300348559) if not self.dev_mode else self.get_channel(
            1065101040770363442)
        self.bot_bunker_channel = self.get_channel(1000465465572864141) if not self.dev_mode else self.get_channel(
            1065101076002508800)

        self.league_committee_channel = self.get_channel(1052324453188632696) if not self.dev_mode else self.get_channel(
            1065101182525259866)
        self.side_quest_pools_channel = self.get_channel(1055515435073806387)
        self.stip_channel = self.get_channel(1029841228604375120) if not self.dev_mode else self.get_channel(
            1065101076002508800)
        self.num_boosters_awaiting = 0
        self.awaiting_boosters_for_user = None
        self.spreadsheet_id = self.config.spreadsheet_id
        self.pool_tracker = PoolTracker(self.pool_channel, self.packs_channel, self.spreadsheet_id, self.pools_tab_id)
        self.second_pool_tracker = self.config.second_spreadsheet_id and PoolTracker(self.pool_channel, self.second_packs_channel, self.config.second_spreadsheet_id, self.pools_tab_id)
        self.matchmaker = Matchmaker("!lfm", "a match", self.lfm_channel, self.spreadsheet_id)
        self.stip_matchmaker = Matchmaker("!stipmatch", "a Wheel of Chaos match", self.stip_channel, self.spreadsheet_id, choose_stip)
        self.matchmakers = [self.matchmaker, self.stip_matchmaker]
        for user in self.users:
            if user.name == 'Booster Tutor':
                self.booster_tutor = user
        self.sheet = await get_sheet_client()
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
            if before.channel == self.pool_channel and "SealedDeck.Tech link" not in before.content and\
                    "SealedDeck.Tech Link" in after.content:
                # Edit adds a sealeddeck link
                await self.track_starting_pool(after)
                return
            elif before.channel == self.packs_channel and not has_pack(before) and has_pack(after):
                # track multiple packs in pack-gen channel
                await self.pool_tracker.track_pack(after)
                return
            elif before.channel == self.second_packs_channel and not has_pack(before) and has_pack(after):
                # track multiple packs in pack-gen channel
                await self.second_pool_tracker.track_pack(after)
                return

    async def on_message(self, message: discord.Message):
        # As part of the !playerchoice flow, repost Booster Tutor packs in pack-generation with instructions for
        # the appropriate user to select their pack.
        if (message.channel == self.bot_bunker_channel and message.author == self.booster_tutor
                and message.mentions[0] == self.user):
            await self.handle_booster_tutor_response(message)
            return

        if message.author == self.booster_tutor:
            if message.channel == self.packs_channel and has_pack(message):
                # Message is a generated pack
                await self.pool_tracker.track_pack(message)
                return
            elif message.channel == self.second_packs_channel and has_pack(message):
                # Message is a generated pack
                await self.second_pool_tracker.track_pack(message)
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

        matchmaker = next((mm for mm in self.matchmakers if message.channel == mm.channel and command == "!challenge"), None)
        if matchmaker:
            await matchmaker.issue_challenge(message)
        elif command == '!help':
            await message.channel.send(
                f"You can give me one of the following commands:\n"
                f"> `!challenge`: Challenges the current player in the LFM (or duel) queue\n"
                f"> `!randint A B`: Generates a random integer n, where A <= n <= B. If only one input is given, "
                f"uses that value as B and defaults A to 1. \n "
                f"> `!help`: shows this message\n"
            )

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
            re.search(r"(?P<url>https?://[^\s]+)", message.content).group("url").split('sealeddeck.tech/')[1]
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


    async def pool_from_changes(self, changes: Sequence[Tuple[str, str, str]]) -> Sequence[SealedDeckEntry]:
        packs = []
        removed_packs = []
        cards: defaultdict[str, int] = defaultdict(int)

        for (_name, operation, value) in changes:
            if operation == "add pack":
                packs.append(value)
            elif operation == "remove pack":
                removed_packs.append(value)
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

        removed_pack_contents = []
        for id in removed_packs:
            removed_pack_contents.append(await sealeddeck_pool(id))
        for pack in removed_pack_contents:
            if pack:
                for card in pack:
                    cards[card["name"]] -= card["count"]

        return [{"name": name, "count": count} for name, count in cards.items() if count > 0]

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
        await self.pool_tracker.track_pack(chosen_message)

        return

    async def on_dm(self, message: discord.Message, command: str, argument: str):
        if command == '!choosepacka' or command == '!chooseurza':
            await self.choose_pack(message.author, 'A')
            return

        if command == '!choosepackb' or command == '!choosemishra':
            await self.choose_pack(message.author, 'B')
            return

        for matchmaker in self.matchmakers:
            if command == matchmaker.command:
                await matchmaker.handle_command(message, argument)
                return

        if command == '!retractlfm' or command == '!nvm':
            handled = False
            for matchmaker in self.matchmakers:
                result = await matchmaker.handle_retract(message)
                handled = handled or result
            if not handled:
                await message.author.send(
                    "You don't currently have an outgoing LFM."
                )
            return

        await message.author.send(
            f"I'm sorry, but I didn't understand that. Please send one of the following commands:\n"
            f"> `{self.matchmaker.command}`: creates an anonymous post looking for {self.matchmaker.what_it_is}.\n"
            f"> `{self.stip_matchmaker.command}`: creates an anonymous post looking for {self.stip_matchmaker.what_it_is}.\n"
            f"> `!nvm`: removes an anonymous LFM that you've sent out.\n"
            f"> `!choosePackA`: responds to a pending pack selection option.\n"
            f"> `!choosePackB`: responds to a pending pack selection option."
        )

    async def add_pack(self, message: discord.Message, argument: str):
        if message.channel != self.packs_channel:
            return

        ref = message.reference and message.reference.message_id and await message.channel.fetch_message(
            message.reference.message_id
        )
        if not ref:
            return
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
        await get_spreadsheet_values(self.spreadsheet_id, range, valueRenderOption)
