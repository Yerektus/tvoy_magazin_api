import re

from rest_framework import serializers

from .models import Extension


class ExtensionLinkSerializer(serializers.ModelSerializer):
    """Ссылка на соседнее расширение — этого хватает, чтобы его показать."""

    class Meta:
        model = Extension
        fields = ('slug', 'name', 'logo')


class ExtensionSerializer(serializers.ModelSerializer):
    """Расширение для каталога и его страницы: только то, что читает человек."""

    description = serializers.SerializerMethodField()
    features = serializers.SerializerMethodField()
    requires = ExtensionLinkSerializer(many=True, read_only=True)
    # Кто стоит поверх: их отключит вместе с этим расширением.
    required_by = ExtensionLinkSerializer(many=True, read_only=True)

    class Meta:
        model = Extension
        fields = (
            'slug',
            'name',
            'summary',
            'description',
            'logo',
            'features',
            'requires',
            'required_by',
        )

    def get_description(self, extension) -> list[str]:
        """Абзацы: в админке их разделяют пустой строкой."""

        return [part.strip() for part in re.split(r'\n\s*\n', extension.description) if part.strip()]

    def get_features(self, extension) -> list[str]:
        return [feature.text for feature in extension.features.all()]
