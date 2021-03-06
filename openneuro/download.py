import sys
import hashlib
import asyncio
from pathlib import Path
from typing import Optional, Iterable, Union
if sys.version_info >= (3, 8):
    from typing import Literal
else:
    from typing_extensions import Literal

import httpx
from tqdm.asyncio import tqdm
import click
import aiofiles

from . import __version__
from .config import default_base_url


try:
    sys.stdout.reconfigure(encoding='utf-8')
    stdout_unicode = True
except AttributeError:
    # Python 3.6
    stdout_unicode = False


# HTTP server responses that indicate hopefully intermittent errors that
# warrant a retry.
allowed_retry_codes = (408, 500, 502, 503, 504, 522, 524)
allowed_retry_exceptions = (httpx.ConnectTimeout, httpx.ReadTimeout)


def _get_download_metadata(*,
                           base_url: str,
                           dataset_id: str,
                           tag: Optional[str] = None,
                           max_retries: int,
                           retry_backoff: float) -> dict:
    """Retrieve dataset metadata required for the download.
    """
    if tag is None:
        url = f'{base_url}crn/datasets/{dataset_id}/download'
    else:
        url = f'{base_url}crn/datasets/{dataset_id}/snapshots/{tag}/download'

    try:
        response = httpx.get(url)
        request_timed_out = False
    except allowed_retry_exceptions:
        request_timed_out = True

    if request_timed_out and max_retries > 0:
        tqdm.write(f'Request timed out, retrying …')
        asyncio.sleep(retry_backoff)
        max_retries -= 1
        retry_backoff *= 2
        return _get_download_metadata(base_url=base_url, dataset_id=dataset_id,
                                      tag=tag, max_retries=max_retries,
                                      retry_backoff=retry_backoff)
    elif request_timed_out:
        raise RuntimeError('Timeout when trying to fetch metadata.')
    
    if not response.is_error:
        response_json = response.json()
        return response_json
    elif response.status_code in allowed_retry_codes and max_retries > 0:
        tqdm.write(f'Error {response.status_code}, retrying …')
        asyncio.sleep(retry_backoff)
        max_retries -= 1
        retry_backoff *= 2
        return _get_download_metadata(base_url=base_url, dataset_id=dataset_id,
                                      tag=tag, max_retries=max_retries,
                                      retry_backoff=retry_backoff)
    else:
        raise RuntimeError(f'Error {response.status_code} when trying to '
                           f'fetch metadata.')


async def _download_file(*,
                         url: str,
                         api_file_size: int,
                         outfile: Path,
                         verify_hash: bool,
                         verify_size: bool,
                         max_retries: int,
                         retry_backoff: float,
                         semaphore: asyncio.Semaphore) -> None:
    """Download an individual file.
    """
    if outfile.exists():
        local_file_size = outfile.stat().st_size
    else:
        local_file_size = 0

    # Check if we need to resume a download
    # The file sizes provided via the API often do not match the sizes reported
    # by the HTTP server. Rely on the sizes reported by the HTTP server.
    async with semaphore:
        async with httpx.AsyncClient() as client:
            try:
                async with client.stream('HEAD', url=url) as response:
                    headers = response.headers
            except allowed_retry_exceptions:
                if max_retries > 0:
                    await _retry_download(
                        url=url, outfile=outfile,
                        api_file_size=api_file_size,
                        verify_hash=verify_hash, verify_size=verify_size,
                        max_retries=max_retries,
                        retry_backoff=retry_backoff, semaphore=semaphore)
                    return
                else:
                    raise RuntimeError(f'Timeout when trying to download '
                                        f'{outfile}.')

            # Try to get the S3 MD5 hash for the file.
            try:
                remote_file_hash = headers['etag'].strip('"')
                if len(remote_file_hash) != 32:  # It's not an MD5 hash.
                    remote_file_hash = None
            except KeyError:
                remote_file_hash = None

            # Get the Content-Length.
            try:
                remote_file_size = int(response.headers['content-length'])
            except KeyError:
                # The server doesn't always set a Conent-Length header.
                try:
                    async with client.stream('GET', url=url) as response:
                        response_content = await response.aread()
                        remote_file_size = len(response_content)
                except allowed_retry_exceptions:
                    if max_retries > 0:
                        await _retry_download(
                            url=url, outfile=outfile,
                            api_file_size=api_file_size,
                            verify_hash=verify_hash, verify_size=verify_size,
                            max_retries=max_retries,
                            retry_backoff=retry_backoff, semaphore=semaphore)
                        return
                    else:
                        raise RuntimeError(f'Timeout when trying to download '
                                            f'{outfile}.')

    headers = {}
    if outfile.exists() and local_file_size == remote_file_size:
        hash = hashlib.md5()

        if verify_hash and remote_file_hash is not None:
            async with aiofiles.open(outfile, 'rb') as f:
                while True:
                    data = await f.read(65536)
                    if not data:
                        break
                    hash.update(data)

        if (verify_hash and
                remote_file_hash is not None and
                hash.hexdigest() != remote_file_hash):
            desc = f'Re-downloading {outfile.name}: file hash mismatch.'
            mode = 'wb'
            outfile.unlink()
            local_file_size = 0
        else:
            # Download complete, skip.
            desc = f'Skipping {outfile.name}: already downloaded.'
            t = tqdm(iterable=response.aiter_bytes(),
                     desc=desc,
                     initial=local_file_size,
                     total=remote_file_size, unit='B',
                     unit_scale=True,
                     unit_divisor=1024, leave=False)
            t.close()
            return
    elif outfile.exists() and local_file_size < remote_file_size:
        # Download incomplete, resume.
        desc = f'Resuming {outfile.name}'
        headers['Range'] = f'bytes={local_file_size}-'
        mode = 'ab'
    elif outfile.exists():
        # Local file is larger than remote – overwrite.
        desc = f'Re-downloading {outfile.name}: file size mismatch.'
        mode = 'wb'
        outfile.unlink()
        local_file_size = 0
    else:
        # File doesn't exist locally, download entirely.
        desc = outfile.name
        mode = 'wb'

    async with semaphore:
        async with httpx.AsyncClient() as client:
            try:
                async with (
                    client.stream('GET', url=url, headers=headers)
                ) as response:
                    if not response.is_error:
                        pass  # All good!
                    elif (response.status_code in allowed_retry_codes and
                        max_retries > 0):
                        await _retry_download(
                            url=url, outfile=outfile,
                            api_file_size=api_file_size,
                            verify_hash=verify_hash, verify_size=verify_size,
                            max_retries=max_retries,
                            retry_backoff=retry_backoff, semaphore=semaphore)
                        return
                    else:
                        raise RuntimeError(
                            f'Error {response.status_code} when trying '
                            f'to download {outfile} from {url}')

                    await _retrieve_and_write_to_disk(
                        response=response, outfile=outfile, mode=mode,
                        desc=desc,
                        local_file_size=local_file_size,
                        remote_file_size=remote_file_size,
                        remote_file_hash=remote_file_hash,
                        verify_hash=verify_hash, verify_size=verify_size)
            except allowed_retry_exceptions:
                if max_retries > 0:
                    await _retry_download(
                        url=url, outfile=outfile,
                        api_file_size=api_file_size,
                        verify_hash=verify_hash, verify_size=verify_size,
                        max_retries=max_retries,
                        retry_backoff=retry_backoff, semaphore=semaphore)
                    return
                else:
                    raise RuntimeError(f'Timeout when trying to download '
                                       f'{outfile}.')

async def _retry_download(
    *,
    url: str,
    outfile: Path,
    api_file_size: int,
    verify_hash: bool,
    verify_size: bool,
    max_retries: int,
    retry_backoff: float,
    semaphore: asyncio.Semaphore
) -> None:
    tqdm.write('Request timed out, retrying …')
    await asyncio.sleep(retry_backoff)
    max_retries -= 1
    retry_backoff *= 2
    await _download_file(url=url,
                        api_file_size=api_file_size,
                        outfile=outfile,
                        verify_hash=verify_hash,
                        verify_size=verify_size,
                        max_retries=max_retries,
                        retry_backoff=retry_backoff,
                        semaphore=semaphore)


async def _retrieve_and_write_to_disk(
    *,
    response: httpx.Response,
    outfile: Path,
    mode: Literal['ab', 'wb'],
    desc: str,
    local_file_size: int,
    remote_file_size: int,
    remote_file_hash: Optional[str],
    verify_hash: bool,
    verify_size: bool
) -> None:
    hash = hashlib.md5()

    # If we're resuming a download, ensure the already-downloaded
    # parts of the file are fed into the hash function before
    # we continue.
    if verify_hash and local_file_size > 0:
        async with aiofiles.open(outfile, 'rb') as f:
            while True:
                data = await f.read(65536)
                if not data:
                    break
                hash.update(data)

    async with aiofiles.open(outfile, mode=mode) as f:
        with tqdm(desc=desc, initial=local_file_size,
                    total=remote_file_size, unit='B',
                    unit_scale=True, unit_divisor=1024,
                    leave=False) as progress:
            num_bytes_downloaded = response.num_bytes_downloaded

            # TODO Add timeout handling here, too.
            async for chunk in response.aiter_bytes():
                await f.write(chunk)
                progress.update(response.num_bytes_downloaded -
                                num_bytes_downloaded)
                num_bytes_downloaded = (response
                                        .num_bytes_downloaded)
                if verify_hash:
                    hash.update(chunk)

        if verify_hash and remote_file_hash is not None:
            assert hash.hexdigest() == remote_file_hash

        # Check the file was completely downloaded.
        if verify_size:
            await f.flush()
            local_file_size = outfile.stat().st_size
            if not local_file_size == remote_file_size:
                raise RuntimeError(
                    f'Server claimed file size would be {remote_file_size} '
                    f'bytes, but downloaded {local_file_size} byes.')


async def _download_files(*,
                          target_dir: Path,
                          files: Iterable,
                          verify_hash: bool,
                          verify_size: bool,
                          max_retries: int,
                          retry_backoff: float,
                          max_concurrent_downloads: int) -> None:
    """Download files, one by one.
    """
    # Sempahore (counter) to limit maximum number of concurrent download
    # coroutines.
    semaphore = asyncio.Semaphore(max_concurrent_downloads)
    download_tasks = []

    for file in files:
        filename = Path(file['filename'])
        api_file_size = file['size']
        url = file['urls'][0]

        outfile = target_dir / filename
        outfile.parent.mkdir(parents=True, exist_ok=True)
        download_task = _download_file(
            url=url, api_file_size=api_file_size,
            outfile=outfile, verify_hash=verify_hash,
            verify_size=verify_size, max_retries=max_retries,
            retry_backoff=retry_backoff, semaphore=semaphore)
        download_tasks.append(download_task)

    await asyncio.gather(*download_tasks)


def download(*,
             dataset: str,
             tag: Optional[str] = None,
             target_dir: Optional[Union[Path, str]] = None,
             include: Optional[Iterable[str]] = None,
             exclude: Optional[Iterable[str]] = None,
             verify_hash: bool = True,
             verify_size: bool = True,
             max_retries: int = 5,
             max_concurrent_downloads: int = 5) -> None:
    """Download datasets from OpenNeuro.

    Parameters
    ----------
    dataset
        The dataset to retrieve, for example ``ds000248``.
    tag
        The tag (revision) of the dataset to retrieve.
    target_dir
        The directory in which to store the downloaded data. If ``None``,
        create a subdirectory with the dataset name in the current working
        directory.
    include
        Files and directories to download. **Only** these files and directories
        will be retrieved.
    exclude
        Files and directories to exclude from downloading.
    verify_hash
        Whether to calculate and print the SHA256 hash of each downloaded file.
    verify_size
        Whether to check if the downloaded file size matches what the server
        announced.
    max_retries
        Try the specified number of times to download a file before failing.
    max_concurrent_downloads
        The maximum number of downloads to run in parallel.
    """
    msg_problems = 'problems 🤯' if stdout_unicode else 'problems'
    msg_bugs = 'bugs 🪲' if stdout_unicode else 'bugs'
    msg_hello = '👋 Hello!' if stdout_unicode else 'Hello!'
    msg_great_to_see_you = 'Great to see you!'
    if stdout_unicode:
        msg_great_to_see_you += ' 🤗'
    msg_please = '👉 Please' if stdout_unicode else '   Please'
        
    msg = (f'\n{msg_hello} This is openneuro-py {__version__}. '
           f'{msg_great_to_see_you}\n\n'
           f'   {msg_please} report {msg_problems} and {msg_bugs} at\n'
           f'      https://github.com/hoechenberger/openneuro-py/issues\n')
    tqdm.write(msg)

    msg = f'Preparing to download {dataset}'
    if stdout_unicode:
        msg = f'🌍 {msg} …'
    else:
        msg += ' ...'
    tqdm.write(msg)

    if target_dir is None:
        target_dir = Path(dataset)
    else:
        target_dir = Path(target_dir)

    include = [] if include is None else list(include)
    exclude = [] if exclude is None else list(exclude)

    retry_backoff = 0.5  # seconds
    metadata = _get_download_metadata(base_url=default_base_url,
                                      dataset_id=dataset,
                                      tag=tag,
                                      max_retries=max_retries,
                                      retry_backoff=retry_backoff)

    files = []
    include_counts = [0] * len(include)  # Keep track of include matches.
    for file in metadata['files']:
        filename: str = file['filename']  # TODO properly define metadata type

        # Always include essential BIDS files.
        if filename in ('dataset_description.json',
                        'participants.tsv',
                        'participants.json',
                        'README',
                        'CHANGES'):
            files.append(file)
            # Keep track of include matches.
            if filename in include:
                include_counts[include.index(filename)] += 1
            continue

        if ((not include or any(filename.startswith(i) for i in include)) and
                not any(filename.startswith(e) for e in exclude)):
            files.append(file)
            # Keep track of include matches.
            for i in include:
                if filename.startswith(i):
                    include_counts[include.index(i)] += 1

    for idx, count in enumerate(include_counts):
        if count == 0:
            raise RuntimeError(f'Could not find paths starting with '
                               f'{include[idx]} in the dataset. Please '
                               f'check your includes.')

    msg = (f'Retrieving up to {len(files)} files '
           f'({max_concurrent_downloads} concurrent downloads).')
    if stdout_unicode:
        msg = f'👉 {msg}'
    tqdm.write(msg)

    # Pre-Python-3.7 compat. Once we drop support for Python 3.6, simply
    # replace this with: asyncio.run(_download_files(...))
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.wait(
        [_download_files(
            target_dir=target_dir,
            files=files,
            verify_hash=verify_hash,
            verify_size=verify_size,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            max_concurrent_downloads=max_concurrent_downloads)]
    ))

    msg_finished = f'Finished downloading {dataset}.'
    if stdout_unicode:
        msg_finished = f'✅ {msg_finished}'
    tqdm.write(msg_finished)

    msg_enjoy = 'Please enjoy your brains.'
    if stdout_unicode:
        msg_enjoy = f'\n🧠 {msg_enjoy}'
    else:
        msg_enjoy = f'\n{msg_enjoy}'
    msg_enjoy += '\n'
    tqdm.write(msg_enjoy)


@click.command()
@click.option('--dataset', required=True, help='The OpenNeuro dataset name.')
@click.option('--tag', help='The tag (version) of the dataset.')
@click.option('--target_dir', help='The directory to download to.')
@click.option('--include', multiple=True,
              help='Only include the specified file or directory. Can be '
                   'passed multiple times.')
@click.option('--exclude', multiple=True,
              help='Exclude the specified file or directory. Can be passed '
                   'multiple times.')
@click.option('--verify_hash', type=bool, default=True, show_default=True,
              help='Whether to print the SHA256 hash of each downloaded file.')
@click.option('--verify_size', type=bool, default=True, show_default=True,
              help='Whether to check the downloaded file size matches what '
                   'the server announced.')
@click.option('--max_retries', type=int, default=5, show_default=True,
              help='Try the specified number of times to download a file '
                   'before failing.')
@click.option('--max_concurrent_downloads', type=int, default=5,
              show_default=True,
              help='The maximum number of downloads to run in parallel.')
def download_cli(**kwargs):
    """Download datasets from OpenNeuro."""
    download(**kwargs)
