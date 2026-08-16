from django.contrib import admin

from .models import Extension, ExtensionFeature


class ExtensionFeatureInline(admin.TabularInline):
    model = ExtensionFeature
    extra = 1


@admin.register(Extension)
class ExtensionAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'summary', 'is_active', 'position')
    list_editable = ('is_active', 'position')
    search_fields = ('name', 'slug', 'summary')
    list_filter = ('is_active',)
    prepopulated_fields = {'slug': ('name',)}
    inlines = [ExtensionFeatureInline]
