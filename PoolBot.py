from __future__ import print_function
# bot.py
import os

import discord
import re
import random
import ssl
import time
from dotenv import load_dotenv
from typing import Optional, Sequence, Union, List, TypedDict, Tuple, Any
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


class PoolBotError(Exception):
    """Base exception for PoolBot errors"""
    pass


class SealedDeckError(PoolBotError):
    """Error when sealeddeck.tech API fails"""
    pass


class SpreadsheetError(PoolBotError):
    """Error when spreadsheet operations fail"""
    pass


def parse_sealeddeck_url(content: str) -> Optional[str]:
    """Extract sealeddeck ID from message content. Returns None if not found."""
    match = re.search(r"https?://(?:www\.)?sealeddeck\.tech/([^/\s]+)", content)
    return match.group(1) if match else None


class SealedDeckEntry(TypedDict):
    name: str
    count: int


# Spreadsheet data structures
class PlayerDatabaseRow(TypedDict):
    name: str
    discord_id: int
    hero_score: float


class PoolChangeRow(TypedDict):
    name: str
    operation: str
    value: str
    pool_id: str


class PoolRow(TypedDict, total=False):
    """Row from Pools tab - not all columns are always present"""
    name: str
    pool_id: str
    pack_count: int
    map_count: int
    maps_used: int
    maps_remaining: int


def parse_hero_score(value: str) -> float:
    """Parse Hero Score from column AE. Empty or invalid values become 0."""
    stripped = value.strip()
    if not stripped:
        return 0.0
    try:
        return float(stripped)
    except ValueError:
        return 0.0


def parse_player_row(row: list[str]) -> Optional[PlayerDatabaseRow]:
    """Parse a player database row. Returns None if row is invalid."""
    # Column AE (index 30) contains Hero Score: A=0, ..., Z=25, AA=26, ..., AE=30
    if len(row) < 31:
        return None
    try:
        return {
            "name": row[0],
            "discord_id": int(row[3]),
            "hero_score": parse_hero_score(row[30]),
        }
    except (ValueError, IndexError):
        return None


def format_coin_flip_note(
    pending_name: str,
    pending_score: float,
    challenger_name: str,
    challenger_score: float,
) -> str:
    """Describe hero scores and who wins the coin flip (higher score wins)."""
    note = (
        f" {pending_name} has Hero Score {pending_score:g}, "
        f"{challenger_name} has Hero Score {challenger_score:g}."
    )
    if pending_score > challenger_score:
        note += f" {pending_name} wins the coin flip and chooses whether to play first."
    elif challenger_score > pending_score:
        note += f" {challenger_name} wins the coin flip and chooses whether to play first."
    else:
        note += " Hero Scores are tied — flip a coin to decide who plays first."
    return note


def parse_pool_change_row(row: list[str]) -> Optional[PoolChangeRow]:
    """Parse a pool change row. Returns None if row is invalid."""
    if len(row) < 5:
        return None
    return {
        "name": str(row[0]),
        "operation": str(row[1]),
        "value": str(row[2]),
        "pool_id": str(row[4]) if len(row) > 4 and row[4] else "",
    }


def parse_pool_row(row: list[str]) -> Optional[PoolRow]:
    """Parse a pool row. Returns None if row is invalid."""
    if len(row) < 1:
        return None
    pool_row: PoolRow = {"name": str(row[0])}
    if len(row) > 4:
        pool_row["pool_id"] = str(row[4])
    if len(row) > 15:
        try:
            pool_row["maps_used"] = int(row[15])
            pool_row["maps_remaining"] = int(row[16])
        except (ValueError, IndexError):
            pass
    return pool_row


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

async def sealeddeck_pool(pool_sealeddeck_id: str) -> Sequence[SealedDeckEntry]:
    """Fetch pool data from sealeddeck.tech. Raises SealedDeckError on failure."""
    resp_json = None

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{SEALEDDECK_URL}/{pool_sealeddeck_id}") as resp:
                    resp.raise_for_status()
                    resp_json = await resp.json()
        except Exception as e:
            if attempt == 2:
                raise SealedDeckError(f"Failed to fetch pool {pool_sealeddeck_id} after 3 attempts: {e}")
            continue
        else:
            break

    if resp_json is None:
        raise SealedDeckError(f"Received null response for pool {pool_sealeddeck_id}")

    return [*resp_json["sideboard"], *resp_json["deck"], *resp_json["hidden"]]

async def pool_to_sealeddeck(
        punishment_cards: Sequence[SealedDeckEntry], pool_sealeddeck_id: Optional[str] = None
) -> str:
    """Adds punishment cards to a sealeddeck.tech pool and returns the id. Raises SealedDeckError on failure."""
    deck: dict[str, Union[Sequence[SealedDeckEntry], str]] = {"sideboard": punishment_cards}
    if pool_sealeddeck_id:
        deck["poolId"] = pool_sealeddeck_id

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(SEALEDDECK_URL, json=deck) as resp:
                    resp.raise_for_status()
                    resp_json = await resp.json()
        except Exception as e:
            if attempt == 2:
                raise SealedDeckError(f"Failed to create pool after 3 attempts: {e}")
            continue
        else:
            break

    return str(resp_json["poolId"])


async def update_message(message: discord.Message, new_content: str) -> Optional[discord.Message]:
    """Updates the text contents of a sent bot message. Returns None on failure."""
    try:
        result = await message.edit(content=new_content)
        return result
    except discord.errors.Forbidden:
        print(f"Could not edit message {message.id} - permission denied")
        return None
    except Exception as e:
        print(f"Could not edit message {message.id}: {e}")
        return None


async def message_member(member: Union[discord.Member, discord.User], message: str):
    try:
        await member.send(message)
        # await member.send(
        #     "Greetings, current or former Arena Gauntlet League player! This is your last chance to join us for the Wilds of Eldraine league before registration closes on Wednesday, September 6th at 5pm EST.\n\nSign up here: https://docs.google.com/forms/d/e/1FAIpQLSe44aHmif2QsplYoxdyKDmrpj6hRhywdPLQD4SYhOvhvjfsGA/viewform.\n\nWe hope to see you there!")
        time.sleep(0.25)
    except discord.errors.Forbidden as e:
        print(e)

async def get_sheet_client() -> Any:
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
        raise

async def get_spreadsheet_values(sheet: Any, spreadsheet_id: str, range: str, valueRenderOption="FORMATTED_VALUE") -> list[list[str]]:
    """Fetch spreadsheet values with retry logic for transient errors. Raises SpreadsheetError on permanent failure."""
    retries = 0
    while retries < 5:
        try:
            # Call the Sheets API
            result = sheet.values().get(spreadsheetId=spreadsheet_id,
                                        range=range,
                                        valueRenderOption=valueRenderOption).execute()
            return result.get('values', []) or []
        except (ssl.SSLEOFError, HttpError) as err:
            retries += 1
            if retries >= 5:
                raise SpreadsheetError(f"Failed to fetch {range} after 5 retries: {err}")
            print(f"Spreadsheet error (attempt {retries}/5): {err}")
            await sleep(retries)
    return []

async def get_sheet_title_by_id(spreadsheet: Any, spreadsheet_id: str, tab_id: str) -> str:
    """Resolve a numeric sheet tab ID to its title for A1 range notation."""
    tab_id_int = int(tab_id)
    try:
        result = spreadsheet.get(
            spreadsheetId=spreadsheet_id,
            fields='sheets(properties(sheetId,title))',
        ).execute()
    except HttpError as err:
        raise SpreadsheetError(f"Failed to fetch spreadsheet metadata: {err}")
    for sheet in result.get('sheets', []):
        props = sheet.get('properties', {})
        if props.get('sheetId') == tab_id_int:
            return props['title']
    raise SpreadsheetError(f"Sheet tab id {tab_id} not found in spreadsheet")

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
    try:
        sheet.batchUpdate(spreadsheetId=spreadsheet_id,
                          body=color_body).execute()
    except HttpError as e:
        print(f"spreadsheet error — setting cell to red: {e}")
        raise SpreadsheetError(f"Failed to set cell to red: {e}")

class PoolTracker():
    def __init__(self, sheet: Any, pool_channel: discord.TextChannel, packs_channel: discord.TextChannel, spreadsheet_id: str, tab_id: str):
        self.sheet = sheet
        self.pool_channel = pool_channel
        self.packs_channel = packs_channel
        self.spreadsheet_id = spreadsheet_id
        self.tab_id = tab_id
        self.pool_lock = Lock()

    async def track_pack(self, message: discord.Message):
        """
        Track a pack in the Pools tab. This assumes the pack's owner is the last mention in the message, and that the pack contents is in a code fence.
        """
        async with self.pool_lock:
            content = message.embeds[0].description
            ref = message.reference and message.reference.message_id and await message.channel.fetch_message(message.reference.message_id)
            pack_owner_user_id_match = ref and re.search("<@!?(?P<id>\\d+)>", ref.content)
            pack_owner_user_id = pack_owner_user_id_match and pack_owner_user_id_match.group("id")

            # Get and parse pool changes from spreadsheet
            try:
                raw_changes = await get_spreadsheet_values(self.sheet, self.spreadsheet_id, 'Pool Changes!B2:F')
            except SpreadsheetError as e:
                print(f"spreadsheet error — fetching changes: {e}")
                await self.set_cell_to_red(0, 'G')  # Can't determine row, use 0
                return
            changes = [c for c in (parse_pool_change_row(r) for r in raw_changes) if c is not None]

            # Find player in database (name is column B, discord ID is column F)
            try:
                raw_player_data = await get_spreadsheet_values(self.sheet, self.spreadsheet_id, "Player Database!B2:F")
            except SpreadsheetError as e:
                print(f"spreadsheet error — fetching player data: {e}")
                await self.set_cell_to_red(0, 'G')
                return
            player_data = [p for p in (parse_player_row(r) for r in raw_player_data) if p is not None]
            if pack_owner_user_id is None:
                raise ValueError("Could not extract user ID from message reference")
            player_match = next(((i, p) for i, p in enumerate(player_data) if p["discord_id"] == int(pack_owner_user_id)), (None, None))
            player_row_index, player_row = player_match

            if player_row is None or player_row_index is None:
                # This should only happen during debugging / spreadsheet setup
                print(f"rut row. No pool found for {pack_owner_user_id}")
                raise ValueError(f"No pool found for {pack_owner_user_id}")
            
            # pool row starts at 7 (1-indexed), player rows start at 0, so add 7 to the index
            row_num = player_row_index + 7
            name = player_row["name"]

            # current pool is last pool in the changes that matches the player name
            current_pool_id = ''
            for change in changes:
                if change["name"] == name and change["pool_id"]:
                    current_pool_id = change["pool_id"]

            if current_pool_id == '':
                print(f"rut row. No pool found for {name}")
                await self.set_cell_to_red(row_num, 'G')
                raise ValueError(f"No pool found for {name}")

            # either it's a single pack or there's a Sealeddeck ID
            if content and "```" in content:
                pack_content = content.split("```")[1].strip()
                pack_json = arena_to_json(pack_content)
            else:
                field = next(filter(lambda f: f.name == "SealedDeck.Tech ID", message.embeds[0].fields))
                field_value = field.value or ""
                try:
                    pack_json = await sealeddeck_pool(field_value.replace("`", ""))
                except SealedDeckError as e:
                    print(f"sealeddeck error — fetching pack: {e}")
                    await self.set_cell_to_red(row_num, 'G')
                    return

            try:
                new_pack_id = await pool_to_sealeddeck(pack_json)
                updated_pool_id = await pool_to_sealeddeck(pack_json, current_pool_id)
            except SealedDeckError as e:
                print(f"sealeddeck error — updating pool: {e}")
                await self.set_cell_to_red(row_num, 'G')
                return

            try:
                await self.write_pack(name, new_pack_id, updated_pool_id)
            except SpreadsheetError as e:
                print(f"spreadsheet error — writing pack: {e}")
                await self.set_cell_to_red(row_num, 'G')
                return

    async def write_pack(self, name: str, new_pack_id: str, updated_pool_id: str):
        pack_body = {
            'values': [
                [datetime.now().isoformat(),name,"add pack",new_pack_id,"",updated_pool_id],
            ],
        }
        # Find the proper column ID
        try:
            self.sheet.values().append(spreadsheetId=self.spreadsheet_id,
                                       range=f'Pool Changes!A:D', valueInputOption='USER_ENTERED',
                                       body=pack_body).execute()
        except HttpError as e:
            print(f"spreadsheet error — writing pack: {e}")
            raise SpreadsheetError(f"Failed to write pack to spreadsheet: {e}")

    async def set_cell_to_red(self, row: int, col: str):
        await set_cell_to_red(self.sheet, self.spreadsheet_id, self.tab_id, row, col)

class Matchmaker():
    def __init__(
        self,
        sheet: Any,
        command: str,
        what_it_is: str,
        channel: discord.TextChannel,
        spreadsheet_id: str,
        player_database_tab_id: str,
        extra=None,
    ):
        self.sheet = sheet
        self.command = command
        self.what_it_is = what_it_is
        self.channel = channel
        self.spreadsheet_id = spreadsheet_id
        self.player_database_tab_id = player_database_tab_id
        self.extra = extra
        self.pending_user_mention: Optional[str] = None
        self.pending_user_id: Optional[int] = None
        self.active_message: Optional[discord.Message] = None
        self._player_database_tab_name: Optional[str] = None

    async def _fetch_player_data(self) -> list[PlayerDatabaseRow]:
        if self._player_database_tab_name is None:
            self._player_database_tab_name = await get_sheet_title_by_id(
                self.sheet, self.spreadsheet_id, self.player_database_tab_id
            )
        tab_name = self._player_database_tab_name.replace("'", "''")
        player_range = f"'{tab_name}'!A2:AE"
        raw_player_data = await get_spreadsheet_values(
            self.sheet, self.spreadsheet_id, player_range
        )
        return [p for p in (parse_player_row(r) for r in raw_player_data) if p is not None]

    async def issue_challenge(self, message: discord.Message):
        """Handle challenge command. Early returns if no pending user or active message."""
        if not self.pending_user_mention:
            await self.channel.send(
                f"Sorry, but no one is looking for {self.what_it_is} right now. You can send out an anonymous LFM by DMing me "
                f"`{self.command}`. "
            )
            return
        if self.active_message is None:
            return  # Shouldn't happen if pending_user_mention is set, but guard anyway
        async with self.channel.typing():
            try:
                player_data = await self._fetch_player_data()
            except SpreadsheetError as e:
                print(f"spreadsheet error — fetching player data for matchmaking: {e}")
                await self.channel.send(
                    f"Sorry, I couldn't look up player data to resolve the coin flip. "
                    f"Please try again or contact the league committee."
                )
                return

            def get_player(discord_id: Optional[int]) -> Optional[PlayerDatabaseRow]:
                if discord_id is None:
                    return None
                return next((p for p in player_data if p["discord_id"] == discord_id), None)

            pending_player = get_player(self.pending_user_id)
            challenger_player = get_player(message.author.id)
            pending_name = pending_player["name"] if pending_player else self.pending_user_mention or "LFM player"
            challenger_name = challenger_player["name"] if challenger_player else message.author.display_name
            pending_score = pending_player["hero_score"] if pending_player else 0.0
            challenger_score = challenger_player["hero_score"] if challenger_player else 0.0
            coin_flip_note = format_coin_flip_note(
                pending_name, pending_score, challenger_name, challenger_score
            )
            pending_user_extra = ""
            challenger_extra = ""
            overall_extra = self.extra() if self.extra else ""

            try:
                await self.channel.send(
                    f"{self.pending_user_mention}, your anonymous LFM has been accepted by "
                    f"{message.author.mention}.{coin_flip_note}{overall_extra}"
                )
            except Exception as e:
                print(f"Failed to send match announcement: {e}, keeping player pending")
                # State remains intact, player stays pending
                return

            # Announcement sent - match is complete. Update old post is cosmetic.
            await update_message(
                self.active_message,
                f'~~{self.active_message.content}~~\n'
                f'A match was found between {self.pending_user_mention} and '
                f'{message.author.mention}.{coin_flip_note}'
            )

            # Clear state - match is done regardless of whether update_message succeeded
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
        active_message = self.active_message
        await message.author.send(
            f"I've created a post for you: {active_message.jump_url}\n"
            "You'll receive a mention when an opponent is found.\n"
            f"If you want to cancel this, send me a message with the text `!nvm`."
        )
        self.pending_user_mention = message.author.mention
        self.pending_user_id = int(message.author.id)

    async def handle_retract(self, message: discord.Message) -> bool:
        if message.author.mention == self.pending_user_mention and self.active_message is not None:
            await self.active_message.delete()
            self.active_message = None
            await message.author.send(
                f"Understood. The post made on your behalf in {self.channel.jump_url} has been deleted."
            )
            self.pending_user_mention = None
            self.pending_user_id = None
            return True
        return False

def has_pack(message: discord.Message) -> bool:
    """Check if message contains pack data. Raises IndexError if no embeds."""
    embed = message.embeds[0]  # Let IndexError propagate if no embeds
    has_code_block = embed.description and "```" in embed.description
    has_field = any(f.name == "SealedDeck.Tech ID" for f in embed.fields)
    return has_code_block or has_field

class PoolBot(discord.Client):
    def __init__(self, config: utils.Config, intents: discord.Intents, *args, **kwargs):
        self.config = config
        self.league_start = datetime.fromisoformat('2022-06-22')
        super().__init__(intents=intents, *args, **kwargs)

    def _get_channel(self, channel_id: int) -> discord.TextChannel:
        """Get a channel by ID, validating it exists and is a TextChannel. Raises on failure."""
        channel = self.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"Required channel {channel_id} not found or is not a TextChannel")
        return channel

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        if not self.config.skip_username and self.user is not None:
            result = await self.user.edit(username=self.config.bot_name)
            if result is None:
                raise RuntimeError("Failed to update bot username")
        # If this is true, posts will be limited to #bot-lab and #bot-bunker, and LFM DMs will be ignored.
        self.dev_mode = self.config.debug_mode == "active"
        self.pools_tab_id = self.config.pools_tab_id

        # Get all required channels at startup - fail fast if any are missing
        self.pool_channel = self._get_channel(self.config.pool_channel_id)
        self.packs_channel = self._get_channel(self.config.packs_channel_id)
        self.second_packs_channel = self._get_channel(self.config.second_packs_channel_id)
        self.lfm_channel = self._get_channel(self.config.lfm_channel_id)
        self.bot_bunker_channel = self._get_channel(self.config.bot_bunker_channel_id)
        self.league_committee_channel = self._get_channel(self.config.league_committee_channel_id)
        self.side_quest_pools_channel = self._get_channel(self.config.side_quest_pools_channel_id)
        self.num_boosters_awaiting = 0
        self.awaiting_boosters_for_user: Optional[Union[discord.Member, discord.User]] = None
        self.spreadsheet_id = self.config.spreadsheet_id

        # Get sheet client first - fail fast if it fails
        self.sheet = await get_sheet_client()

        # Pass sheet to PoolTracker - explicit dependencies
        self.pool_tracker = PoolTracker(self.sheet, self.pool_channel, self.packs_channel, self.spreadsheet_id, self.pools_tab_id)
        self.second_pool_tracker: Optional[PoolTracker] = PoolTracker(self.sheet, self.pool_channel, self.second_packs_channel, self.config.second_spreadsheet_id, self.pools_tab_id) if self.config.second_spreadsheet_id else None

        self.matchmaker = Matchmaker(
            self.sheet,
            "!lfm",
            "a match",
            self.lfm_channel,
            self.spreadsheet_id,
            self.config.player_database_tab_id,
        )
        self.matchmakers = [self.matchmaker]
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
            if before.channel == self.pool_channel and "SealedDeck.Tech link" not in before.content and\
                    "SealedDeck.Tech Link" in after.content:
                # Edit adds a sealeddeck link
                await self.track_starting_pool(after)
                return
            elif before.channel == self.packs_channel and not has_pack(before) and has_pack(after):
                # track multiple packs in pack-gen channel
                await self.pool_tracker.track_pack(after)
                return
            elif before.channel == self.second_packs_channel and not has_pack(before) and has_pack(after) and self.second_pool_tracker is not None:
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
            elif message.channel == self.second_packs_channel and has_pack(message) and self.second_pool_tracker is not None:
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
        # Get and parse pool data from spreadsheet
        raw_pools = await self.get_spreadsheet_values('Pools!B7:R200')
        pools = [p for p in (parse_pool_row(r) for r in raw_pools) if p is not None]
        curr_row = 6
        for pool in pools:
            curr_row += 1
            pool_name = pool.get("name")
            if not pool_name or pool_name.lower() not in message.author.display_name.lower():
                continue
            maps_remaining = pool.get("maps_remaining", 0)
            if maps_remaining <= 0:
                await message.reply(f'By my records, you do not have any unused maps. If this is in error, '
                                    f'please post in {self.league_committee_channel.mention}')
                return

            # Mark the map as used
            maps_used = pool.get("maps_used", 0)
            self.sheet.values().update(spreadsheetId=self.spreadsheet_id,
                                       range=f'Pools!Q{curr_row}:Q{curr_row}', valueInputOption='USER_ENTERED',
                                       body={'values': [[maps_used + 1]]}).execute()

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

        # Parse sealeddeck URL at boundary
        sealed_deck_id = parse_sealeddeck_url(message.content)
        if sealed_deck_id is None:
            return
        sealed_deck_link = f'https://sealeddeck.tech/{sealed_deck_id}'

        # Get and parse pool data from spreadsheet
        try:
            raw_pools = await self.get_spreadsheet_values('Pools!B7:F200')
        except SpreadsheetError as e:
            print(f"spreadsheet error — fetching pools: {e}")
            return
        pools = [p for p in (parse_pool_row(r) for r in raw_pools) if p is not None]
        curr_row = 6
        for pool in pools:
            curr_row += 1
            pool_name = pool.get("name")
            if not pool_name or pool_name.lower() not in message.mentions[0].display_name.lower():
                continue
            # Update the proper cell in the spreadsheet
            body = {
                'values': [
                    [sealed_deck_link, sealed_deck_link],
                ],
            }
            try:
                self.sheet.values().update(spreadsheetId=self.spreadsheet_id,
                                           range=f'Pools!E{curr_row}:F{curr_row}', valueInputOption='USER_ENTERED',
                                           body=body).execute()
                self.sheet.values().update(spreadsheetId=self.spreadsheet_id,
                                           range=f'Pools!S{curr_row}:S{curr_row}', valueInputOption='USER_ENTERED',
                                           body={'values': [[sealed_deck_link]]}).execute()
            except HttpError as e:
                print(f"spreadsheet error — updating pool: {e}")
                return

            return
        # TODO do something if the value could not be found
        return


    async def pool_from_changes(self, changes: Sequence[Tuple[str, str, str]]) -> Sequence[SealedDeckEntry]:
        """Reconstruct pool from change history. Raises SealedDeckError if any pack fetch fails."""
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

        # Fetch and aggregate pack contents - sealeddeck_pool raises on failure
        for pack_id in packs:
            pack = await sealeddeck_pool(pack_id)
            for card in pack:
                cards[card["name"]] += card["count"]

        for pack_id in removed_packs:
            pack = await sealeddeck_pool(pack_id)
            for card in pack:
                cards[card["name"]] -= card["count"]

        return [{"name": name, "count": count} for name, count in cards.items() if count > 0]

    async def prompt_user_pick(self, message: discord.Message):
        # # Ensure the user doesn't already have a pending pick to make
        # pendingPickMessage = await self.packs_channel.history().find(
        # 	lambda m : m.author.name == self.config.bot_name
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
        """Handle booster tutor pack generation response. Assumes called only when awaiting boosters."""
        assert self.num_boosters_awaiting > 0, "Called without pending boosters"
        assert self.awaiting_boosters_for_user is not None, "No user awaiting boosters"
        user = self.awaiting_boosters_for_user
        if self.num_boosters_awaiting == 2:
            self.num_boosters_awaiting -= 1
            await self.packs_channel.send(
                f'Pack Option A for {user.mention}. To select this pack, DM me '
                f'`!choosePackA`\n '
                f'```{message.content.split("```")[1].strip()}```')
        else:
            self.num_boosters_awaiting -= 1
            await self.packs_channel.send(
                f'Pack Option B for {user.mention}. To select this pack, DM me '
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
        chosen_message: Optional[discord.Message] = None
        async for message in self.packs_channel.history(limit=500):
            if (message.author.name == self.config.bot_name and message.mentions and message.mentions[0] == user
                    and f'Pack Option {chosen_option}' in message.content):
                chosen_message = message
                break

        not_chosen_message: Optional[discord.Message] = None
        async for message in self.packs_channel.history(limit=500):
            if (message.author.name == self.config.bot_name and message.mentions and message.mentions[0] == user
                    and f'Pack Option {not_chosen_option}' in message.content):
                not_chosen_message = message
                break

        if chosen_message is None or not_chosen_message is None:
            await user.send(
                f"Sorry, but I couldn't find any pending packs for you. Please post in "
                f"{self.league_committee_channel.mention} if you think this is an error.")
            return

        chosen_message_text = f'Pack chosen by {user.mention}.{chosen_message.content.split(split)[1]}'

        updated_chosen = await update_message(chosen_message, chosen_message_text)
        if updated_chosen is None:
            await user.send("Sorry, I couldn't update the pack selection message. Please try again or contact the league committee.")
            return

        not_chosen_text = f'Pack not chosen by {user.mention}.' f'~~{not_chosen_message.content.split(not_chosen_split)[1]}~~'
        await update_message(not_chosen_message, not_chosen_text)  # Best effort, don't fail if this doesn't work

        await user.send("Understood. Your selection has been noted.")

        # TODO this likely breaks because booster tutor messages and ours don't follow the same format anymore (embed vs content)
        await self.pool_tracker.track_pack(updated_chosen)

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

        matchmaker_help = "\n".join(
            f"> `{mm.command}`: creates an anonymous post looking for {mm.what_it_is}." for mm in self.matchmakers
        )
        await message.author.send(
            f"I'm sorry, but I didn't understand that. Please send one of the following commands:\n"
            f"{matchmaker_help}\n"
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
            new_id = await pool_to_sealeddeck(pack_json, sealeddeck_id)
        except SealedDeckError as e:
            print(f"Sealeddeck error: {e}")
            content = (
                f"{message.author.mention}\n"
                f"The packs could not be added to sealeddeck.tech "
                f"pool with ID `{sealeddeck_id}`. Please, verify "
                f"the ID.\n"
                f"If the ID is correct, sealeddeck.tech might be "
                f"having some issues right now, try again later."
            )
            await m.edit(content=content)
            return

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

    async def message_members(self, message: str = ""):
        for member in self.guilds[0].members:
            if member.display_name in 'put names here':
                print('trying to DM: ' + member.display_name)
                # if 'Sawyer T' in member.display_name:
                await message_member(member, message)
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

    async def get_spreadsheet_values(self, range: str, valueRenderOption="FORMATTED_VALUE") -> list[list[str]]:
        return await get_spreadsheet_values(self.sheet, self.spreadsheet_id, range, valueRenderOption)
