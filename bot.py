import asyncio
import functools
import itertools
import math
import random

import others
import botset
import musics

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands

# Silence useless bug reports messages
youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Aha da bu çalıyo',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Süre', value=self.source.duration)
                 .add_field(name='İstek Parça Sahibi', value=self.requester.mention)
                 .add_field(name='Şarkı Sahibi', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                try:
                    async with timeout(5400):  # 90 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('Bu komut özel mesaj olarak kullanılamaz')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('Buralarda bi hata var: {}'.format(str(error)))#error message

    @commands.command(name=musics.comjoin, invoke_without_subcommand=True)#join command name in musics.py
    async def _join(self, ctx: commands.Context):
        """Çağırıldığı kanala gelir""" #join commands description

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name=musics.comsum)#summon command name in musics.py
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """Bir kanala bağlıysa farklı bir odaya çağırmak için kullanabilirsin"""#summon commands description

        if not channel and not ctx.author.voice:
            raise VoiceError(musics.errsum)#error message for summons error in musics.py

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name=musics.comlv)#leave command name in musics.py
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Listeyi temizleyip gider"""#leave commands description

        if not ctx.voice_state.voice:
            return await ctx.send(musics.errlv)#error message for leave commands error in musics.py

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name=musics.comvol)#volume command name in musics.py
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Sesini düzenler"""#volume commands description

        if not ctx.voice_state.is_playing:
            return await ctx.send(musics.errvol)#error message for volume in musics.py

        if 0 > volume > 100:
            return await ctx.send(musics.msgvol1)#'pls enter a value between 0-100'message in musics.py

        ctx.voice_state.volume = volume / 100
        await ctx.send(musics.msgvol2.format(volume))#confirm message in musics.py

    @commands.command(name=musics.comnow)#now command name in musics.py
    async def _now(self, ctx: commands.Context):
        """Şu an ne çaldığını merak ediyosan sor"""#now commands description

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name=musics.compau)#pause command name in musics.py
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Susar ve çaldığı şarkıyı duraklatır"""#pause commands description

        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction(musics.msgpau)#message for pause in musics.py

    @commands.command(name=musics.comres)#resume command name in musics.py
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Şarkı çalmaya kaldığı yerden devam eder"""#resume commands description

        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction(musics.msgres)#message for resume in musics.py

    @commands.command(name=musics.comstp)#stop command name in musics.py
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Ne çalıyosa bırakır ve susar"""#stop commands description

        ctx.voice_state.songs.clear()

        if not ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction(musics.msgstp)#message for stop in musics.py

    @commands.command(name=musics.comskp)#skip command name in musics.py
    async def _skip(self, ctx: commands.Context):
        """Şarkıya skip atmak için.
        İstek parça sahibi tek oyla değiştirebilir. Geri kalanlar 3 oy toplamak zorunda.
        """#skip commands description

        if not ctx.voice_state.is_playing:
            return await ctx.send(musics.errskp)#error message for skip command in musics.py

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction(musics.reaskp1)#reaction for skip command in musics.py
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.send(musics.reaskp2)#confirm skip reaction in musics.py
                ctx.voice_state.skip()
            else:
                await ctx.send(musics.msgskp1.format(total_votes))#info skip message in musics.py

        else:
            await ctx.send(musics.msgskp2)#'only 1 vote!' message in musics.py

    @commands.command(name=musics.comque)#queue command name in musics.py
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Şarkı listesini görüntüler.
        Sıradaki 10 şarkıyı görüntüleyebilir"""#queue commands description

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send(musics.errque)#error message for queue in musics.py

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Görüntülenen Sayfa {}/{}'.format(page, pages)))#list page
        await ctx.send(embed=embed)

    @commands.command(name=musics.comshf)#shuffle command name in musics.py
    async def _shuffle(self, ctx: commands.Context):
        """Şarkı listesini karıştırır"""#shuffle commands description

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send(musics.errshf)#error message for shuffle in musics.py

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction(musics.reashf)#confirm shuffle reaction in musics.py

    @commands.command(name=musics.comrem)#remove command name in musics.py
    async def _remove(self, ctx: commands.Context, index: int):
        """Listeden silmek istediğin şarkının index numarasını gir"""#remove commands description

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send(musics.errrem)#error message for remove in muscis.py

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction(musics.rearem)#confirm remove reaction in musics.py

    @commands.command(name=musics.comlop)#loop command name in musics.py
    async def _loop(self, ctx: commands.Context):
        """Şarkıyı sonsuza kadar çalar. İptal etmek için unloop yaz"""#loop commands description

        if not ctx.voice_state.is_playing:
            return await ctx.send(musics.errlop)#error message for loop in musics.py

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction(musics.realop)#confirm loop reaction in musics.py

    @commands.command(name=musics.comply)#play command name in musics.py
    async def _play(self, ctx: commands.Context, *, search: str):
        """Bi şarkı çalar.
        url ya da isim arayabilirsin.
        """#play commands description

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send(musics.errply1.format(str(e)))#error message for play in musics.py
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send(musics.msgply.format(str(source)))#confirm message for play in musics.py

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError(musics.errjoin)#'you are not in a voice channel'message for join in musics.py

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError(musics.errjoin2)#'i am already in a voice channel'message for join in musics.py


class Othercommands(commands.Cog):
    @commands.command(name=others.com1)#your command name 1 in others.py
    async def com1(self, ctx: commands.Context):
        await ctx.send(others.des1)#your commands description 1 in others.py

    @commands.command(name=others.com2)#your command name 2 in others.py
    async def com2(self, ctx: commands.Context):
        await ctx.send(others.des2)#your commands description 2 in others.py

    @commands.command(name=others.com3)#your command name 3 in others.py
    async def com3(self, ctx: commands.Context):
        await ctx.send(others.des3)#your commands description 3 in others.py

    @commands.command(name=others.com4)#your commands description 4 in others.py
    async def com4(self, ctx: commands.Context):
        await ctx.send(others.des4)#your commands description 4 in others.py


bot = commands.Bot(command_prefix=botset.bot_prefix, description=botset.bot_description)#It is your command prefix and bot description in botset.py
bot.add_cog(Music(bot))
bot.add_cog(Othercommands(bot))


@bot.event
async def on_ready():
    print('Giriş yapıldı:\n{0.user.name}\n{0.user.id}'.format(bot))

bot.run(botset.bot_token)