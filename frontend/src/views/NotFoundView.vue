<script setup lang="ts">
/**
 * 404 view — kept inside the app shell so navigation stays available.
 */
import { useRouter } from 'vue-router'
import { Back, HomeFilled } from '@element-plus/icons-vue'

const router = useRouter()

function goHome(): void {
  void router.push('/generate')
}

function goBack(): void {
  if (window.history.length > 1) router.back()
  else goHome()
}
</script>

<template>
  <div class="t2v-page not-found-view">
    <el-card shadow="never" class="not-found-view__card">
      <el-result icon="warning" title="404" sub-title="抱歉，你访问的页面不存在或已被移动。">
        <template #extra>
          <div class="not-found-view__actions">
            <el-button type="primary" :icon="HomeFilled" @click="goHome">返回语音生成</el-button>
            <el-button :icon="Back" @click="goBack">返回上一页</el-button>
          </div>
        </template>
      </el-result>
    </el-card>
  </div>
</template>

<style scoped>
.not-found-view {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: calc(100vh - var(--t2v-header-height) - 2 * var(--t2v-content-padding));
}

.not-found-view__card {
  width: 100%;
  max-width: 520px;
}

.not-found-view__actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  flex-wrap: wrap;
}
</style>