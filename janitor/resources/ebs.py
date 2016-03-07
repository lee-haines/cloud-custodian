import itertools
import logging

from concurrent.futures import as_completed

from janitor.actions import ActionRegistry, BaseAction
from janitor.filters import (
    FilterRegistry, ValueFilter, ANNOTATION_KEY)

from janitor.manager import ResourceManager, resources
from janitor import tags
from janitor.utils import (
    local_session, set_annotation, query_instances, chunks)


log = logging.getLogger('maid.ebs')

filters = FilterRegistry('ebs.filters')
actions = ActionRegistry('ebs.actions')

tags.register_tags(filters, actions, 'VolumeId')


@resources.register('ebs')
class EBS(ResourceManager):

    def __init__(self, ctx, data):
        super(EBS, self).__init__(ctx, data)
        self.filters = filters.parse(
            self.data.get('filters', []), self)
        self.actions = actions.parse(
            self.data.get('actions', []), self) 

    def resources(self):
        c = self.session_factory().client('ec2')
        query = self.resource_query()
        if self._cache.load():
            vols = self._cache.get({'resource': 'ebs', 'q': query})
            if vols is not None:
                self.log.debug("Using cached ebs: %d" % len(vols))
                return self.filter_resources(vols)
        self.log.info("Querying ebs volumes")
        p = c.get_paginator('describe_volumes')
        results = p.paginate(Filters=query)
        volumes = list(itertools.chain(*[rp['Volumes'] for rp in results]))
        self._cache.save({'resource': 'ebs', 'q': query}, volumes)
        return self.filter_resources(volumes)

    
@filters.register('instance')
class AttachedInstanceFilter(ValueFilter):
    """Filter volumes based on filtering on their attached instance"""

    def process(self, resources, event=None):
        original_count = len(resources)
        resources = [r for r in resources if r.get('Attachments')]
        self.log.debug('Filtered from %d volumes to %d attached volumes' % (
            original_count, len(resources)))
        self.instance_map = self.get_instance_mapping(resources)
        return filter(self, resources)
    
    def __call__(self, r):
        instance = self.instance_map[r['Attachments'][0]['InstanceId']]
        if self.match(instance):
            r['Instance'] = instance
            set_annotation(r, ANNOTATION_KEY, "instance-%s" % self.k)
            return True
        
    def get_instance_mapping(self, resources):
        instance_ids = [r['Attachments'][0]['InstanceId'] for r in resources]
        instances = query_instances(
            local_session(self.manager.session_factory),
            InstanceIds=instance_ids)
        return {i['InstanceId']: i for i in instances}
        

@actions.register('copy-instance-tags')
class CopyInstanceTags(BaseAction):
    """Copy instance tags to its attached volume.

    Useful for cost allocation to ebs volumes and tracking usage
    info for volumes.

    Mostly useful for volumes not set to delete on termination, which
    are otherwise candidates for garbage collection, copying the
    instance tags gives us more semantic information to determine if
    their useful, as well letting us know the last time the volume
    was actually used.
    """
    def process(self, volumes):
        volumes = [v for v in volumes if v['Attachments']]
        with self.executor_factory(max_workers=10) as w:
            futures = []
            for volume_set in chunks(reversed(volumes), size=100):
                futures.append(
                    w.submit(self.process_volume_set, volume_set))

                for f in as_completed(futures):
                    if f.exception():
                        self.log.error(
                            "Exception copying instance tags \n %s" % (
                                f.exception()))

    def process_volume_set(self, volume_set):
        instance_vol_map = {}
        for v in volume_set:
            instance_vol_map.setdefault(
                v['Attachments'][0]['InstanceId'], []).append(v)

        instance_map = {i['InstanceId']: i for i in query_instances(
            Filters=[
                {'Name': 'instance-id', 'Values': instance_vol_map.keys()}])}

        for i in instance_vol_map:
            try:
                self.process_instance_volumes(
                    instance_map[i], instance_vol_map[i])
            except Exception as e:
                self.log.exception(
                    "Error copying instance tags to volumes \n %s" % e)
        
    def process_instance_volumes(self, instance, volumes):
        client = local_session(self.manager.session_factory).client('ec2')
        
        for v in volumes:
            copy_tags = self.get_volume_tags(v, instance, v['Attachments'][0])
            if not copy_tags:
                continue
            # Can't add more tags than the resource supports could try
            # to delete extant ones inline, else trim-tags action.
            if len(copy_tags) > 10:
                log.warning(
                    "action:%s volume:%s instance:%s too many tags to copy" % (
                        self.__class__.__name__.lower(),
                        v['VolumeId'], instance['InstanceId']))
                continue
        
            client.create_tags(
                Resources=[v['VolumeId']],
                Tags=copy_tags,
                DryRun=self.manager.config.dryrun)

    def get_volume_tags(self, volume, instance, attachment):
        only_tags = self.data.get('tags', [])  # specify which tags to copy
        copy_tags = []
        extant_tags = dict([
            (t['Key'], t['Value']) for t in volume.get('Tags', [])])
        
        for t in instance['Tags']:
            if only_tags and not t['Key'] in only_tags:
                continue
            if t['Key'] in extant_tags and t['Value'] == extant_tags[t['Key']]:
                continue
            if t['Key'].startswith('aws:'):
                continue
            copy_tags.append(t)

        # Don't add attachment tags if we're already current
        if 'LastAttachInstance' in extant_tags \
           and extant_tags['LastAttachInstance'] == attachment['InstanceId']:
            return copy_tags
            
        copy_tags.append(
            {'Key': 'LastAttachTime',
             'Value': attachment['AttachTime'].isoformat()})
        copy_tags.append(
            {'Key': 'LastAttachInstance', 'Value': attachment['InstanceId']})
        return copy_tags


@actions.register('encrypt-instance-volumes')
class EncryptInstanceVolumes(BaseAction):
    """Encrypt extant volumes attached to an instance

    - Requires instance restart
    - Not suitable for autoscale groups.
    """

    def validate(self):
        key = self.data.get('key')
        if not key:
            raise ValueError(
                "action:encrypt-instance-volume "
                "requires kms keyid/alias specified")
        self.verbose = self.data.get('verbose', False)
        return self

    def process(self, volumes):
        original_count = len(volumes)
        volumes = [v for v in volumes
                   if not v['Encrypted'] or not v['Attachments']]
        log.debug(
            "EncryptVolumes filtered from %d to %d "
            " unencrypted attached volumes" % (
                original_count, len(volumes)))
        
        # Group volumes by instance id
        instance_vol_map = {}
        for v in volumes:
            instance_id = v['Attachments'][0]['InstanceId']
            instance_vol_map.setdefault(instance_id, []).append(v)

        with self.executor_factory(max_workers=10) as w:
            futures = {}
            for instance_id, vol_set in instance_vol_map.items():
                futures[w.submit(
                    self.process_volume, instance_id, vol_set)] = instance_id
                
            for f in as_completed(futures):
                if f.exception():
                    instance_id = futures[f]
                    log.error(
                        "Exception processing instance:%s volset: %s \n %s" % (
                            instance_id, instance_vol_map[instance_id],
                            f.exception()))

    def process_volume(self, instance_id, vol_set):
        """Encrypt attached unencrypted ebs volume

        vol_set corresponds to all the unencrypted volumes on a given instance.

        Multistep process
        
        - Stop instance
        - For each volume
          - Create snapshot
          - Wait on snapshot creation
          - Copy Snapshot to create encrypted snapshot
          - Wait on snapshot creation
          - Create encrypted volume from snapshot
          - Wait on volume creation
          - Delete transient snapshots
          - Detach Unencrypted Volume
          - Attach Encrypted Volume
        - For each volume
          - Delete unencrypted volume
        - Start Instance
        """
        client = local_session(self.manager.session_factory).client('ec2')
        client.stop_instances(InstanceIds=[instance_id])
        self.wait_on_resource(client, instance_id=instance_id)
        
        key_id = self.get_encryption_key()
        if self.verbose:
            self.log.debug("Using encryption key: %s" % key_id)

        # Create all the volumes before patching the instance.
        paired = []
        for v in vol_set:
            vol_id = self.create_encrypted_volume(v, key_id, instance_id)
            paired.append((v, vol_id))

        # Next detach and reattach
        for v, vol_id in paired:
            client.detach_volume(
                InstanceId=instance_id, VolumeId=v['VolumeId'])
            client.attach_volume(
                InstanceId=instance_id, VolumeId=vol_id,
                Device=v['Attachments'][0]['Device'])

        client.start_instances(InstanceIds=[instance_id])
        
        if self.verbose:
            self.log.debug(
                "Deleting unencrypted volumes for: %s" % instance_id)
            
        for v in vol_set:
            client.delete_volume(VolumeId=v['VolumeId'])
        
    def create_encrypted_volume(self, v, key_id, instance_id):
        # Create a current snapshot
        ec2 = local_session(self.manager.session_factory).client('ec2')
        results = ec2.create_snapshot(
            VolumeId=v['VolumeId'],
            Description="maid transient snapshot for encryption",)
        transient_snapshots = [results['SnapshotId']]
        ec2.create_tags(
            Resources=[results['SnapshotId']],
            Tags=[
                {'Key': 'maid-crypto-remediation', 'Value': 'true'}])
        self.wait_on_resource(ec2, results['SnapshotId'])        

        # Create encrypted snapshot from current
        results = ec2.copy_snapshot(
            SourceSnapshotId=results['SnapshotId'],
            SourceRegion=v['AvailabilityZone'][:-1],
            Description='maid transient snapshot for encryption',
            Encrypted=True,
            KmsKeyId=key_id)
        transient_snapshots.append(results['SnapshotId'])
        ec2.create_tags(
            Resources=[results['SnapshotId']],
            Tags=[
                {'Key': 'maid-crypto-remediation', 'Value': True}
            ])
        self.wait_on_resource(ec2, results['SnapshotId'])        

        # Create encrypted volume, also tag so we can recover
        results = ec2.create_volume(
            Size=v['Size'],
            VolumeType=v['VolumeType'],
            SnapshotId=results['SnapshotId'],
            AvailabilityZone=v['AvailabilityZone'],
            Encrypted=True)
        ec2.create_tags(
            Resources=[results['VolumeId']],
            Tags=[
                {'Key': 'maid-crypt-remediation', 'Value': instance_id},
                {'Key': 'maid-origin-volume', 'Value': v['VolumeId']},
                {'Key': 'maid-instance-device',
                 'Value': v['Attachments'][0]['Device']}])
        
        # Wait on encrypted volume creation
        self.wait_on_resource(ec2, volume_id=results['VolumeId'])
        
        # Delete transient snapshots
        for sid in transient_snapshots:
            ec2.delete_snapshot(SnapshotId=sid)
        return results['VolumeId']

    def get_encryption_key(self):
        kms = local_session(self.manager.session_factory).client('kms')
        key_alias = self.data.get('key')
        result = kms.describe_key(KeyId=key_alias)
        key_id = result['KeyMetadata']['KeyId']
        return key_id
    
    def wait_on_resource(self, *args, **kw):
        # Sigh this is dirty, but failure in the middle of our workflow
        # due to overly long resource creation is complex to unwind,
        # with multi-volume instances. Wait up to three times (actual
        # wait time is a per resource type configuration.

        # Note we wait for all resource creation before attempting to
        # patch an instance, so even on resource creation failure, the
        # instance is not modified
        try:
            return self._wait_on_resource(*args, **kw)
        except Exception:
            try:
                return self._wait_on_resource(*args, **kw)
            except Exception:
                return self._wait_on_resource(*args, **kw)
        
    def _wait_on_resource(
            self, client, snapshot_id, volume_id, instance_id=None):
        # boto client waiters poll every 15 seconds up to a max 600s (5m)
        if snapshot_id:
            if self.verbose:
                self.log.debug(
                    "Waiting on snapshot completion %s" % snapshot_id)
            waiter = client.get_waiter('snapshot_completed')
            waiter.wait(SnapshotIds=[snapshot_id])
            if self.verbose:
                self.log.debug("Snapshot: %s completed" % snapshot_id)
        elif volume_id:
            if self.verbose:
                self.log.debug("Waiting on volume creation %s" % volume_id)
            waiter = client.get_waiter('volume_available')        
            waiter.wait(VolumeIds=[volume_id])
            if self.verbose:
                self.log.debug("Volume: %s created" % volume_id)
        elif instance_id:
            if self.verbose:
                self.log.debug("Waiting on instance stop")
            waiter = client.get_waiter('instance_stopped')
            waiter.wait(InstanceIds=[instance_id])
            if self.verbose:
                self.log.debug("Instance: %s stopped" % instance_id)
                        
    
@actions.register('delete')
class Delete(BaseAction):

    def process(self, volumes):
        with self.executor_factory(max_workers=10) as w:
            list(w.map(self.process_volume, volumes))
                
    def process_volume(self, volume):
        client = local_session(self.manager.session_factory).client('ec2')
        self._run_api(
            client.delete_volume,
            VolumeId=volume['VolumeId'],
            DryRun=self.manager.config.dryrun)
