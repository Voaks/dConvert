import asyncio
import io
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

CLOUDCONVERT_API_BASE = "https://api.cloudconvert.com/v2"
CLOUDCONVERT_SYNC_BASE = "https://sync.api.cloudconvert.com/v2"
FORMAT_RE = re.compile(r"^[a-z0-9]+$")

OUTPUT_FORMAT_CHOICES = [
    app_commands.Choice(name="PDF document (.pdf)", value="pdf"),
    app_commands.Choice(name="Word document (.docx)", value="docx"),
    app_commands.Choice(name="PowerPoint (.pptx)", value="pptx"),
    app_commands.Choice(name="Excel spreadsheet (.xlsx)", value="xlsx"),
    app_commands.Choice(name="Plain text (.txt)", value="txt"),
    app_commands.Choice(name="PNG image (.png)", value="png"),
    app_commands.Choice(name="JPEG image (.jpg)", value="jpg"),
    app_commands.Choice(name="WebP image (.webp)", value="webp"),
    app_commands.Choice(name="GIF image (.gif)", value="gif"),
    app_commands.Choice(name="MP3 audio (.mp3)", value="mp3"),
    app_commands.Choice(name="WAV audio (.wav)", value="wav"),
    app_commands.Choice(name="OGG audio (.ogg)", value="ogg"),
    app_commands.Choice(name="MP4 video (.mp4)", value="mp4"),
    app_commands.Choice(name="WebM video (.webm)", value="webm"),
    app_commands.Choice(name="MOV video (.mov)", value="mov"),
]


class ConfigError(RuntimeError):
    pass


class CloudConvertError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    discord_token: str
    cloudconvert_api_key: str
    guild_id: int | None
    max_output_upload_mb: int
    cloudconvert_sync_timeout_seconds: int
    conversion_task_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        discord_token = os.getenv("DISCORD_TOKEN")
        cloudconvert_api_key = os.getenv("CLOUDCONVERT_API_KEY")

        if not discord_token:
            raise ConfigError("DISCORD_TOKEN is missing. Add it to your .env file.")
        if not cloudconvert_api_key:
            raise ConfigError("CLOUDCONVERT_API_KEY is missing. Add it to your .env file.")

        guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        try:
            guild_id = int(guild_id_raw) if guild_id_raw else None
        except ValueError as exc:
            raise ConfigError("DISCORD_GUILD_ID must be a numeric Discord server ID.") from exc

        return cls(
            discord_token=discord_token,
            cloudconvert_api_key=cloudconvert_api_key,
            guild_id=guild_id,
            max_output_upload_mb=get_int_env("MAX_OUTPUT_UPLOAD_MB", 25),
            cloudconvert_sync_timeout_seconds=get_int_env("CLOUDCONVERT_SYNC_TIMEOUT_SECONDS", 300),
            conversion_task_timeout_seconds=get_int_env("CONVERSION_TASK_TIMEOUT_SECONDS", 300),
        )


def get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc

    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero.")
    return value


def normalize_format(value: str) -> str:
    normalized = value.lower().strip().lstrip(".")
    if not FORMAT_RE.fullmatch(normalized):
        raise ValueError("Formats can only contain letters and numbers, like `pdf`, `docx`, or `png`.")
    return normalized


def output_filename(input_filename: str, output_format: str) -> str:
    stem = input_filename.rsplit(".", 1)[0] if "." in input_filename else input_filename
    safe_stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" ._") or "converted"
    return f"{safe_stem}.{output_format}"


class CloudConvertClient:
    def __init__(self, api_key: str, session: aiohttp.ClientSession, sync_timeout_seconds: int) -> None:
        self._api_key = api_key
        self._session = session
        self._sync_timeout_seconds = sync_timeout_seconds

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def convert_attachment(
        self,
        *,
        attachment_url: str,
        input_filename: str,
        output_format: str,
        input_format: str | None,
        task_timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        job = await self._create_job(
            attachment_url=attachment_url,
            input_filename=input_filename,
            output_format=output_format,
            input_format=input_format,
            task_timeout_seconds=task_timeout_seconds,
        )

        finished_job = await self._wait_for_job(job["id"])
        if finished_job.get("status") != "finished":
            raise CloudConvertError(self._format_failed_job(finished_job))

        export_task = next(
            (task for task in finished_job.get("tasks", []) if task.get("operation") == "export/url"),
            None,
        )
        files = ((export_task or {}).get("result") or {}).get("files") or []
        if not files:
            raise CloudConvertError("CloudConvert finished the job, but did not return an exported file.")
        return files

    async def _create_job(
        self,
        *,
        attachment_url: str,
        input_filename: str,
        output_format: str,
        input_format: str | None,
        task_timeout_seconds: int,
    ) -> dict[str, Any]:
        convert_task: dict[str, Any] = {
            "operation": "convert",
            "input": "import-file",
            "output_format": output_format,
            "filename": output_filename(input_filename, output_format),
            "timeout": task_timeout_seconds,
        }
        if input_format:
            convert_task["input_format"] = input_format

        payload = {
            "tasks": {
                "import-file": {
                    "operation": "import/url",
                    "url": attachment_url,
                    "filename": input_filename,
                },
                "convert-file": convert_task,
                "export-file": {
                    "operation": "export/url",
                    "input": "convert-file",
                    "archive_multiple_files": True,
                },
            }
        }

        async with self._session.post(
            f"{CLOUDCONVERT_API_BASE}/jobs",
            headers=self._headers,
            json=payload,
        ) as response:
            body = await read_json_response(response)
            if response.status >= 400:
                raise CloudConvertError(format_api_error(body, response.status))
            return body["data"]

    async def _wait_for_job(self, job_id: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self._sync_timeout_seconds)
        async with self._session.get(
            f"{CLOUDCONVERT_SYNC_BASE}/jobs/{job_id}",
            headers=self._headers,
            timeout=timeout,
        ) as response:
            body = await read_json_response(response)
            if response.status >= 400:
                raise CloudConvertError(format_api_error(body, response.status))
            return body["data"]

    @staticmethod
    def _format_failed_job(job: dict[str, Any]) -> str:
        failed_task = next((task for task in job.get("tasks", []) if task.get("status") == "error"), None)
        if not failed_task:
            return f"CloudConvert job ended with status `{job.get('status', 'unknown')}`."

        message = failed_task.get("message") or "The conversion task failed."
        code = failed_task.get("code")
        return f"CloudConvert failed: {message}" + (f" (`{code}`)" if code else "")


async def read_json_response(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        return await response.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError) as exc:
        text = await response.text()
        raise CloudConvertError(f"CloudConvert returned an unexpected response: {text[:300]}") from exc


def format_api_error(body: dict[str, Any], status: int) -> str:
    message = body.get("message") or body.get("error") or "Unknown CloudConvert API error."
    errors = body.get("errors")
    if isinstance(errors, dict):
        first_field = next(iter(errors.values()), None)
        if isinstance(first_field, list) and first_field:
            message = f"{message} {first_field[0]}"
    return f"CloudConvert API error ({status}): {message}"


class FileConversionBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None
        self.cloudconvert: CloudConvertClient | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.cloudconvert = CloudConvertClient(
            self.config.cloudconvert_api_key,
            self.http_session,
            self.config.cloudconvert_sync_timeout_seconds,
        )
        register_commands(self)

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Synced slash commands to guild {self.config.guild_id}.")
        else:
            await self.tree.sync()
            print("Synced global slash commands.")

    async def close(self) -> None:
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} ({self.user.id if self.user else 'unknown id'}).")


def register_commands(bot: FileConversionBot) -> None:
    @bot.tree.command(name="convert", description="Convert an uploaded file with CloudConvert.")
    @app_commands.describe(
        file="The file to convert.",
        output_format="Choose the format to convert the file to.",
        input_format="Optional source format if the filename is ambiguous.",
    )
    @app_commands.choices(output_format=OUTPUT_FORMAT_CHOICES)
    async def convert(
        interaction: discord.Interaction,
        file: discord.Attachment,
        output_format: str,
        input_format: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            target_format = normalize_format(output_format)
            source_format = normalize_format(input_format) if input_format else None
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        assert bot.cloudconvert is not None
        assert bot.http_session is not None

        try:
            exported_files = await bot.cloudconvert.convert_attachment(
                attachment_url=file.url,
                input_filename=file.filename,
                output_format=target_format,
                input_format=source_format,
                task_timeout_seconds=bot.config.conversion_task_timeout_seconds,
            )
            await send_conversion_result(
                interaction=interaction,
                session=bot.http_session,
                exported_files=exported_files,
                max_upload_bytes=bot.config.max_output_upload_mb * 1024 * 1024,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "CloudConvert is still working or timed out. Try a smaller file, or raise `CLOUDCONVERT_SYNC_TIMEOUT_SECONDS`.",
                ephemeral=True,
            )
        except CloudConvertError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except aiohttp.ClientError as exc:
            await interaction.followup.send(f"Network error while converting the file: {exc}", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Unexpected conversion error: {exc}", ephemeral=True)


async def send_conversion_result(
    *,
    interaction: discord.Interaction,
    session: aiohttp.ClientSession,
    exported_files: list[dict[str, Any]],
    max_upload_bytes: int,
) -> None:
    if len(exported_files) > 1:
        links = "\n".join(format_export_link(file) for file in exported_files)
        await interaction.followup.send(f"Converted files are ready:\n{links}")
        return

    exported_file = exported_files[0]
    url = exported_file.get("url")
    filename = exported_file.get("filename") or "converted-file"
    if not url:
        raise CloudConvertError("CloudConvert did not return a download URL.")

    size = await get_content_length(session, url)
    if size is not None and size > max_upload_bytes:
        await interaction.followup.send(
            f"Converted file is ready, but it is too large to upload here:\n{format_export_link(exported_file)}"
        )
        return

    async with session.get(url) as response:
        if response.status >= 400:
            await interaction.followup.send(f"Converted file is ready:\n{format_export_link(exported_file)}")
            return

        data = await response.read()
        if len(data) > max_upload_bytes:
            await interaction.followup.send(
                f"Converted file is ready, but it is too large to upload here:\n{format_export_link(exported_file)}"
            )
            return

    discord_file = discord.File(fp=io.BytesIO(data), filename=filename)
    await interaction.followup.send("Converted file is ready.", file=discord_file)


async def get_content_length(session: aiohttp.ClientSession, url: str) -> int | None:
    try:
        async with session.head(url, allow_redirects=True) as response:
            if response.status >= 400:
                return None
            length = response.headers.get("Content-Length")
            return int(length) if length else None
    except (aiohttp.ClientError, ValueError):
        return None


def format_export_link(exported_file: dict[str, Any]) -> str:
    filename = exported_file.get("filename") or "converted file"
    url = exported_file.get("url") or ""
    return f"- [{filename}]({url})"


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    bot = FileConversionBot(config)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
