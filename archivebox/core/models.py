__package__ = 'archivebox.core'


import uuid
import ulid
import json
import hashlib
from typeid import TypeID

from pathlib import Path
from typing import Optional, List, NamedTuple
from importlib import import_module

from django.db import models
from django.utils.functional import cached_property
from django.utils.text import slugify
from django.core.cache import cache
from django.urls import reverse
from django.db.models import Case, When, Value, IntegerField
from django.contrib.auth.models import User   # noqa

from ..config import ARCHIVE_DIR, ARCHIVE_DIR_NAME
from ..system import get_dir_size
from ..util import parse_date, base_url, hashurl, domain
from ..index.schema import Link
from ..index.html import snapshot_icons
from ..extractors import get_default_archive_methods, ARCHIVE_METHODS_INDEXING_PRECEDENCE, EXTRACTORS

EXTRACTOR_CHOICES = [(extractor_name, extractor_name) for extractor_name in EXTRACTORS.keys()]
STATUS_CHOICES = [
    ("succeeded", "succeeded"),
    ("failed", "failed"),
    ("skipped", "skipped")
]

try:
    JSONField = models.JSONField
except AttributeError:
    import jsonfield
    JSONField = jsonfield.JSONField


class ULIDParts(NamedTuple):
    timestamp: str
    url: str
    subtype: str
    randomness: str


class Tag(models.Model):
    """
    Based on django-taggit model
    """
    id = models.AutoField(primary_key=True, serialize=False, verbose_name='ID')

    name = models.CharField(unique=True, blank=False, max_length=100)

    # slug is autoset on save from name, never set it manually
    slug = models.SlugField(unique=True, blank=True, max_length=100)


    class Meta:
        verbose_name = "Tag"
        verbose_name_plural = "Tags"

    def __str__(self):
        return self.name

    def slugify(self, tag, i=None):
        slug = slugify(tag)
        if i is not None:
            slug += "_%d" % i
        return slug

    def save(self, *args, **kwargs):
        if self._state.adding and not self.slug:
            self.slug = self.slugify(self.name)

            # if name is different but slug conficts with another tags slug, append a counter
            # with transaction.atomic():
            slugs = set(
                type(self)
                ._default_manager.filter(slug__startswith=self.slug)
                .values_list("slug", flat=True)
            )

            i = None
            while True:
                slug = self.slugify(self.name, i)
                if slug not in slugs:
                    self.slug = slug
                    return super().save(*args, **kwargs)
                i = 1 if i is None else i+1
        else:
            return super().save(*args, **kwargs)


class Snapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    url = models.URLField(unique=True, db_index=True)
    timestamp = models.CharField(max_length=32, unique=True, db_index=True)

    title = models.CharField(max_length=512, null=True, blank=True, db_index=True)

    added = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True, blank=True, null=True, db_index=True)
    tags = models.ManyToManyField(Tag, blank=True)

    keys = ('url', 'timestamp', 'title', 'tags', 'updated')

    @property
    def ulid_from_timestamp(self):
        return str(ulid.from_timestamp(self.added))[:10]

    @property
    def ulid_from_urlhash(self):
        return str(ulid.from_randomness(self.url_hash))[10:18]

    @property
    def ulid_from_type(self):
        return '00'

    @property
    def ulid_from_randomness(self):
        return str(ulid.from_uuid(self.id))[20:]

    @property
    def ulid_tuple(self) -> ULIDParts:
        return ULIDParts(self.ulid_from_timestamp, self.ulid_from_urlhash, self.ulid_from_type, self.ulid_from_randomness)

    @property
    def ulid(self):
        return ulid.parse(''.join(self.ulid_tuple))

    @property
    def uuid(self):
        return self.ulid.uuid

    @property
    def typeid(self):
        return TypeID.from_uuid(prefix='snapshot', suffix=self.ulid.uuid)

    def __repr__(self) -> str:
        title = self.title or '-'
        return f'[{self.timestamp}] {self.url[:64]} ({title[:64]})'

    def __str__(self) -> str:
        title = self.title or '-'
        return f'[{self.timestamp}] {self.url[:64]} ({title[:64]})'

    @classmethod
    def from_json(cls, info: dict):
        info = {k: v for k, v in info.items() if k in cls.keys}
        return cls(**info)

    def as_json(self, *args) -> dict:
        args = args or self.keys
        return {
            key: getattr(self, key)
            if key != 'tags' else self.tags_str()
            for key in args
        }

    def as_link(self) -> Link:
        return Link.from_json(self.as_json())

    def as_link_with_details(self) -> Link:
        from ..index import load_link_details
        return load_link_details(self.as_link())

    def tags_str(self, nocache=True) -> str:
        cache_key = f'{self.id}-{(self.updated or self.added).timestamp()}-tags'
        calc_tags_str = lambda: ','.join(self.tags.order_by('name').values_list('name', flat=True))
        if nocache:
            tags_str = calc_tags_str()
            cache.set(cache_key, tags_str)
            return tags_str
        return cache.get_or_set(cache_key, calc_tags_str)

    def icons(self) -> str:
        return snapshot_icons(self)

    @cached_property
    def extension(self) -> str:
        from ..util import extension
        return extension(self.url)

    @cached_property
    def bookmarked(self):
        return parse_date(self.timestamp)

    @cached_property
    def bookmarked_date(self):
        # TODO: remove this
        return self.bookmarked

    @cached_property
    def is_archived(self):
        return self.as_link().is_archived

    @cached_property
    def num_outputs(self):
        return self.archiveresult_set.filter(status='succeeded').count()

    @cached_property
    def url_hash(self):
        # return hashurl(self.url)
        return hashlib.sha256(self.url.encode('utf-8')).hexdigest()[:16].upper()

    @cached_property
    def base_url(self):
        return base_url(self.url)

    @cached_property
    def link_dir(self):
        return str(ARCHIVE_DIR / self.timestamp)

    @cached_property
    def archive_path(self):
        return '{}/{}'.format(ARCHIVE_DIR_NAME, self.timestamp)

    @cached_property
    def archive_size(self):
        cache_key = f'{str(self.id)[:12]}-{(self.updated or self.added).timestamp()}-size'

        def calc_dir_size():
            try:
                return get_dir_size(self.link_dir)[0]
            except Exception:
                return 0

        return cache.get_or_set(cache_key, calc_dir_size)

    @cached_property
    def thumbnail_url(self) -> Optional[str]:
        result = self.archiveresult_set.filter(
            extractor='screenshot',
            status='succeeded'
        ).only('output').last()
        if result:
            return reverse('Snapshot', args=[f'{str(self.timestamp)}/{result.output}'])
        return None

    @cached_property
    def headers(self) -> Optional[dict]:
        try:
            return json.loads((Path(self.link_dir) / 'headers.json').read_text(encoding='utf-8').strip())
        except Exception:
            pass
        return None

    @cached_property
    def status_code(self) -> Optional[str]:
        return self.headers and self.headers.get('Status-Code')

    @cached_property
    def history(self) -> dict:
        # TODO: use ArchiveResult for this instead of json
        return self.as_link_with_details().history

    @cached_property
    def latest_title(self) -> Optional[str]:
        if self.title:
            return self.title   # whoopdedoo that was easy
        
        try:
            # take longest successful title from ArchiveResult db history
            return sorted(
                self.archiveresult_set\
                    .filter(extractor='title', status='succeeded', output__isnull=False)\
                    .values_list('output', flat=True),
                key=lambda r: len(r),
            )[-1]
        except IndexError:
            pass

        try:
            # take longest successful title from Link json index file history
            return sorted(
                (
                    result.output.strip()
                    for result in self.history['title']
                    if result.status == 'succeeded' and result.output.strip()
                ),
                key=lambda r: len(r),
            )[-1]
        except (KeyError, IndexError):
            pass

        return None

    def save_tags(self, tags: List[str]=()) -> None:
        tags_id = []
        for tag in tags:
            if tag.strip():
                tags_id.append(Tag.objects.get_or_create(name=tag)[0].id)
        self.tags.clear()
        self.tags.add(*tags_id)


    def get_storage_dir(self, create=True, symlink=True) -> Path:
        date_str = self.added.strftime('%Y%m%d')
        domain_str = domain(self.url)
        abs_storage_dir = Path(ARCHIVE_DIR) / 'snapshots' / date_str / domain_str / str(self.ulid)

        if create and not abs_storage_dir.is_dir():
            abs_storage_dir.mkdir(parents=True, exist_ok=True)

        if symlink:
            LINK_PATHS = [
                Path(ARCHIVE_DIR).parent / 'index' / 'all_by_id' / str(self.ulid),
                Path(ARCHIVE_DIR).parent / 'index' / 'snapshots_by_id' / str(self.ulid),
                Path(ARCHIVE_DIR).parent / 'index' / 'snapshots_by_domain' / domain_str / date_str / str(self.ulid),
                Path(ARCHIVE_DIR).parent / 'index' / 'snapshots_by_date' / date_str / domain_str / str(self.ulid),
            ]
            for link_path in LINK_PATHS:
                link_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    link_path.symlink_to(abs_storage_dir)
                except FileExistsError:
                    link_path.unlink()
                    link_path.symlink_to(abs_storage_dir)

        return abs_storage_dir

class ArchiveResultManager(models.Manager):
    def indexable(self, sorted: bool = True):
        INDEXABLE_METHODS = [ r[0] for r in ARCHIVE_METHODS_INDEXING_PRECEDENCE ]
        qs = self.get_queryset().filter(extractor__in=INDEXABLE_METHODS,status='succeeded')

        if sorted:
            precedence = [ When(extractor=method, then=Value(precedence)) for method, precedence in ARCHIVE_METHODS_INDEXING_PRECEDENCE ]
            qs = qs.annotate(indexing_precedence=Case(*precedence, default=Value(1000),output_field=IntegerField())).order_by('indexing_precedence')
        return qs


class ArchiveResult(models.Model):
    EXTRACTOR_CHOICES = EXTRACTOR_CHOICES

    id = models.AutoField(primary_key=True, serialize=False, verbose_name='ID')
    uuid = models.UUIDField(default=uuid.uuid4, editable=True)

    snapshot = models.ForeignKey(Snapshot, on_delete=models.CASCADE)
    extractor = models.CharField(choices=EXTRACTOR_CHOICES, max_length=32)
    cmd = JSONField()
    pwd = models.CharField(max_length=256)
    cmd_version = models.CharField(max_length=128, default=None, null=True, blank=True)
    output = models.CharField(max_length=1024)
    start_ts = models.DateTimeField(db_index=True)
    end_ts = models.DateTimeField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)

    objects = ArchiveResultManager()

    def __str__(self):
        return self.extractor

    @cached_property
    def snapshot_dir(self):
        return Path(self.snapshot.link_dir)

    @property
    def ulid_from_timestamp(self):
        return self.snapshot.ulid_from_timestamp

    @property
    def ulid_from_urlhash(self):
        return self.snapshot.ulid_from_urlhash

    @property
    def ulid_from_snapshot(self):
        return str(self.snapshot.ulid)[:18]

    @property
    def ulid_from_type(self):
        return hashlib.sha256(self.extractor.encode('utf-8')).hexdigest()[:2]

    @property
    def ulid_from_randomness(self):
        return str(ulid.from_uuid(self.uuid))[20:]

    @property
    def ulid_tuple(self) -> ULIDParts:
        return ULIDParts(self.ulid_from_timestamp, self.ulid_from_urlhash, self.ulid_from_type, self.ulid_from_randomness)

    @property
    def ulid(self):
        final_ulid = ulid.parse(''.join(self.ulid_tuple))
        # TODO: migrate self.uuid to match this new uuid
        # self.uuid = final_ulid.uuid
        return final_ulid

    @property
    def typeid(self):
        return TypeID.from_uuid(prefix='result', suffix=self.ulid.uuid)

    @property
    def extractor_module(self):
        return EXTRACTORS[self.extractor]

    def output_path(self) -> str:
        """return the canonical output filename or directory name within the snapshot dir"""
        return self.extractor_module.get_output_path()

    def embed_path(self) -> str:
        """
        return the actual runtime-calculated path to the file on-disk that
        should be used for user-facing iframe embeds of this result
        """

        if hasattr(self.extractor_module, 'get_embed_path'):
            return self.extractor_module.get_embed_path(self)

        return self.extractor_module.get_output_path()

    def legacy_output_path(self):
        link = self.snapshot.as_link()
        return link.canonical_outputs().get(f'{self.extractor}_path')

    def output_exists(self) -> bool:
        return Path(self.output_path()).exists()


    def get_storage_dir(self, create=True, symlink=True):
        date_str = self.snapshot.added.strftime('%Y%m%d')
        domain_str = domain(self.snapshot.url)
        abs_storage_dir = Path(ARCHIVE_DIR) / 'results' / date_str / domain_str / str(self.ulid)

        if create and not abs_storage_dir.is_dir():
            abs_storage_dir.mkdir(parents=True, exist_ok=True)

        if symlink:
            LINK_PATHS = [
                Path(ARCHIVE_DIR).parent / 'index' / 'all_by_id' / str(self.ulid),
                Path(ARCHIVE_DIR).parent / 'index' / 'results_by_id' / str(self.ulid),
                Path(ARCHIVE_DIR).parent / 'index' / 'results_by_date' / date_str / domain_str / self.extractor / str(self.ulid),
                Path(ARCHIVE_DIR).parent / 'index' / 'results_by_domain' / domain_str / date_str / self.extractor / str(self.ulid),
                Path(ARCHIVE_DIR).parent / 'index' / 'results_by_type' / self.extractor / date_str / domain_str / str(self.ulid),
            ]
            for link_path in LINK_PATHS:
                link_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    link_path.symlink_to(abs_storage_dir)
                except FileExistsError:
                    link_path.unlink()
                    link_path.symlink_to(abs_storage_dir)

        return abs_storage_dir

    def symlink_index(self, create=True):
        abs_result_dir = self.get_storage_dir(create=create)
