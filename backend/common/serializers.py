from rest_framework import serializers


class StrictInput(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise serializers.ValidationError({"non_field_errors": ["包含不支持的字段"]})
        return super().to_internal_value(data)
